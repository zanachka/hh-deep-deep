import csv
import json
import logging
from pathlib import Path
import re
import subprocess
from typing import List, Optional, Tuple

from .crawl_utils import (
    CrawlPaths, get_last_valid_item, CrawlProcess, gen_job_path)


class DeepDeepPaths(CrawlPaths):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.items = self.root.joinpath('items.jl.gz')
        self.pid = self.root.joinpath('pid.txt')


class DeepDeepProcess(CrawlProcess):
    jobs_root = Path('deep-deep-jobs')
    default_docker_image = 'deep-deep'

    def __init__(self, *,
                 page_clf_data: bytes,
                 root: Path=None,
                 **kwargs):
        super().__init__(**kwargs)
        self.paths = DeepDeepPaths(root or gen_job_path(self.id_, self.jobs_root))
        self.page_clf_data = page_clf_data
        self.last_model_file = None  # last model sent in self.get_new_model

    @classmethod
    def load_running(cls, root: Path, **kwargs) -> Optional['DeepDeepProcess']:
        """ Initialize a process from a directory.
        """
        paths = DeepDeepPaths(root)
        if not all(p.exists() for p in [
                paths.id, paths.pid, paths.seeds, paths.page_clf]):
            return
        pid = paths.pid.read_text()
        try:
            inspect_result = json.loads(subprocess.check_output(
                ['docker', 'inspect', pid]).decode('utf8'))
        except subprocess.CalledProcessError:
            paths.pid.unlink()
            return
        assert len(inspect_result) == 1
        state = inspect_result[0]['State']
        if not state.get('Running'):
            # Remove stopped crawl container and pid file
            paths.pid.unlink()
            subprocess.check_output(['docker', 'rm', pid])
            return
        with paths.seeds.open('rt') as f:
            seeds = [url for url, in csv.reader(f)]
        return cls(
            pid=pid,
            id_=paths.id.read_text(),
            seeds=seeds,
            page_clf_data=paths.page_clf.read_bytes(),
            root=root,
            **kwargs)

    def start(self):
        assert self.pid is None
        self.paths.mkdir()
        self.paths.id.write_text(self.id_)
        self.paths.page_clf.write_bytes(self.page_clf_data)
        with self.paths.seeds.open('wt') as f:
            csv.writer(f).writerows([url] for url in self.seeds)
        args = [
            'docker', 'run', '-d',
            '-v', '{}:{}'.format(self.paths.root, '/job'),
            self.docker_image,
            'scrapy', 'crawl', 'relevant',
            '-a', 'seeds_url=/job/{}'.format(self.paths.seeds.name),
            '-a', 'checkpoint_path=/job',
            '-a', 'classifier_path=/job/{}'.format(self.paths.page_clf.name),
            '-o', 'gzip:/job/items.jl',
            '-a', 'export_cdr=0',
            '--logfile', '/job/spider.log',
            '-L', 'INFO',
            '-s', 'CLOSESPIDER_ITEMCOUNT=1000000',
        ]
        logging.info('Starting crawl in {}'.format(self.paths.root))
        self.pid = subprocess.check_output(args).decode('utf8').strip()
        logging.info('Crawl started, container id {}'.format(self.pid))
        self.paths.pid.write_text(self.pid)

    def stop(self):
        if self.pid:
            subprocess.check_output(['docker', 'stop', self.pid])
            logging.info('Crawl stopped, removing container')
            subprocess.check_output(['docker', 'rm', self.pid])
            self.paths.pid.unlink()
            logging.info('Removed container id {}'.format(self.pid))
            self.pid = None
        else:
            logging.info('Can not stop crawl: it is not running')

    def _get_updates(self) -> Tuple[str, List[str]]:
        if not self.paths.items.exists():
            return 'Craw is not running yet', []
        last_item = get_last_valid_item(str(self.paths.items))
        if last_item is not None:
            return get_updates_from_item(last_item)
        else:
            return 'Crawl started, no updates yet', []

    def get_new_model(self) -> Optional[bytes]:
        """ Return a data of the new model (if there is any), or None.
        """
        model_files = sorted(
            self.paths.root.glob('Q-*.joblib'),
            key=lambda p: int(re.match(r'Q-(\d+)\.joblib', p.name).groups()[0])
        )
        if model_files:
            model_file = model_files[-1]
            if model_file != self.last_model_file:
                self.last_model_file = model_file
                return model_file.read_bytes()


def get_updates_from_item(item):
    url = item.pop('url', None)
    if url:
        page_item = {'url': url}
        reward = item.pop('reward', None)  # type: Optional[float]
        if reward is not None:
            page_item['score'] = 100 * reward
        pages = [page_item]
    else:
        pages = []
    progress = (
        '{pages:,} pages processed from {crawled_domains:,} domains '
        '({relevant_domains:,} relevant), '
        'average score {score:.1f}, '
        '{enqueued:,} requests enqueued, {domains_open:,} domains open.'
        .format(
            pages=item.get('processed', 0),
            crawled_domains=item.get('crawled_domains', 0),
            relevant_domains=item.get('relevant_domains', 0),
            score=(100 * item['return'] / item['t']) if item.get('t') else 0,
            enqueued=item.get('enqueued', 0),
            domains_open=item.get('domains_open', 0),
        )
    )
    return progress, pages