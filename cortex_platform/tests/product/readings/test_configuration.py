"""Configuration and authenticated publication status contracts."""
import json
from pathlib import Path
import sys

import pytest

from cortex_platform.product.api import ControlAPI
from cortex_platform.product.cli import main
from cortex_platform.product.config import ConfigError, initialize, load_config, validate_config, write_config
from cortex_platform.product.control import ControlStore
from cortex_platform.product.paths import resolve_paths
from cortex_platform.product.readings.service import configured_root


@pytest.mark.parametrize('section', [{}, {'papers_root': 'relative'}, {'papers_root': '/'},
                                      {'papers_root': '/papers', 'delete': 'yes'}, {'papers_root': '/tmp/../papers'}])
def test_invalid_configuration(section):
    with pytest.raises(ConfigError):
        validate_config({'config_version': 1, 'readings': section})


def test_config_roundtrip_and_read_only_cli_status(tmp_path, capsys):
    environment = {'HOME': str(tmp_path / 'home')}
    paths = resolve_paths(environ=environment, platform=sys.platform)
    initialize(paths, environ=environment)
    config = load_config(paths.config_file)
    config['readings'] = {'papers_root': str(tmp_path / 'papers')}
    write_config(paths.config_file, config)
    assert load_config(paths.config_file)['readings'] == config['readings']
    assert main(['readings', 'status'], environ=environment, platform=sys.platform) == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'not_started'
    assert not (paths.state_dir / 'readings').exists()


def test_status_requires_authentication(tmp_path):
    store = ControlStore(tmp_path / 'control.db')
    store.initialize()
    api = ControlAPI(store, access_token='x' * 48)
    assert api.handle(method='GET', target='/api/v1/readings', headers={}).status == 403
    response = api.handle(method='GET', target='/api/v1/readings', headers={'X-Cortex-Control-Token': 'x' * 48})
    assert response.status == 200
    assert response.payload == {'enabled': False, 'items': []}


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS boundary')
def test_product_root_overlap_is_rejected(tmp_path):
    paths = resolve_paths(environ={'HOME': str(tmp_path / 'home')}, platform='darwin')
    paths.state_dir.mkdir(parents=True)
    with pytest.raises(ValueError, match='outside product'):
        configured_root({'readings': {'papers_root': str(paths.state_dir)}}, paths)
