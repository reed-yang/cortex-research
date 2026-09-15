"""Real daemon lifecycle: newly adopted sources publish without a capture rerun."""
import json
import os
import sys
import time
from urllib.request import Request, urlopen

import pytest

from cortex_platform.product.config import initialize, load_config, write_config
from cortex_platform.product.control import ControlStore
from cortex_platform.product.lifecycle import start_daemon, stop_daemon
from cortex_platform.product.paths import resolve_paths


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS publication boundary')
def test_daemon_discovers_new_sources_and_exposes_publication_status(tmp_path):
    environment = {**os.environ, 'HOME': str(tmp_path / 'home')}
    paths = resolve_paths(environ=environment, platform='darwin')
    initialize(paths, environ=environment)
    root = tmp_path.resolve() / 'original-papers'
    root.mkdir()
    corpus = paths.data_dir / 'research' / 'corpus'
    paper = corpus / 'fixture-paper'
    paper.mkdir(parents=True)
    (paper / 'notes.md').write_text('source: arxiv:2601.00042')
    (paper / 'full_text.md').write_text('fixture full text')
    store = ControlStore(paths.control_database_file)
    store.initialize()
    store.register_asset_root(root_id='research-corpus', private_path=corpus, max_bytes=1 << 30,
                              enabled=True, actor_id='operator', idempotency_key='readings-daemon-root-0001')
    config = load_config(paths.config_file)
    config['readings'] = {'papers_root': str(root)}
    write_config(paths.config_file, config)
    try:
        assert start_daemon(paths, environ=environment, timeout=10).state == 'running'
        source = store.register_source(authority='arxiv', authority_id='2601.00042', source_kind='paper',
                                       official_title='Fixture', engine_ref='paper:fixture-paper',
                                       actor_id='operator', idempotency_key='readings-daemon-source-0001').value
        deadline = time.monotonic() + 40
        metadata = json.loads(paths.daemon_metadata_file.read_text())
        request = Request(f"http://127.0.0.1:{metadata['port']}/api/v1/readings",
                          headers={'X-Cortex-Control-Token': metadata['control_token']})
        while True:
            with urlopen(request, timeout=3) as response:
                status = json.load(response)
            if any(item['source_id'] == source['id'] and item['state'] == 'published' for item in status['items']):
                break
            assert time.monotonic() < deadline, status
            time.sleep(0.2)
        assert (root / 'fixture-paper' / 'full_text.md').read_text() == 'fixture full text'
        assert (paper / 'full_text.md').read_text() == 'fixture full text'
    finally:
        stop_daemon(paths, timeout=5)
