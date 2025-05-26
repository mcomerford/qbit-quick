import json
import os
import sqlite3
from contextlib import contextmanager
from random import getrandbits
from unittest.mock import mock_open

import pytest
from qbittorrentapi import TrackerStatus, TorrentState, Tracker, TorrentDictionary

from test_helpers import merge_and_remove


@pytest.fixture
def sample_config():
    return {
        'host': 'localhost',
        'port': 1234,
        'username': 'admin',
        'password': 'password',
        'race_categories': 'race',
        'ignore_categories': 'ignore',
        'pausing': True,
        'ratio': 1.0
    }


@pytest.fixture
def override_config(sample_config, request):
    """Fixture that merges sample_config with test-specific updates."""
    config_updates = getattr(request, "param", {})  # Get param from parametrize
    merge_and_remove(sample_config, config_updates)
    return sample_config


@pytest.fixture
def mock_config(monkeypatch, override_config):
    mock_file_content = json.dumps(override_config)
    mocked_open = mock_open(read_data=mock_file_content)
    monkeypatch.setattr("builtins.open", mocked_open)


@pytest.fixture
def mock_config_dir(mocker):
    return mocker.patch('qbitquick.qbit_quick.platformdirs.user_config_dir',
                        return_value=os.path.join('mock', 'config'))


@pytest.fixture
def mock_client_instance(mocker):
    mock = mocker.patch('qbitquick.qbit_quick.Client', autospec=True)
    return mock.return_value


@pytest.fixture
def mock_file_open(mocker):
    mock = mocker.mock_open(read_data='{}')
    mocker.patch('builtins.open', mock)
    return mock


@pytest.fixture
def torrent_factory(mock_client_instance):
    def _create_torrent(**kwargs):
        default_values = {
            'category': 'race',
            'hash': f'%032x' % getrandbits(160),
            'name': 'torrent_name',
            'progress': 0,
            'ratio': 0,
            'state': TorrentState.UNKNOWN,
        }
        default_values.update(kwargs)
        return TorrentDictionary(client=mock_client_instance, data=default_values)
    return _create_torrent


@pytest.fixture
def tracker_factory():
    def _create_tracker(**kwargs):
        default_values = {
            'msg': '',
            'status': TrackerStatus.DISABLED,
            'url': '',
        }
        default_values.update(kwargs)
        return Tracker(default_values)
    return _create_tracker


@pytest.fixture
def mock_get_db_connection(mocker):
    """Mock `get_db_connection` to return an in-memory SQLite database."""
    conn = sqlite3.connect(':memory:')
    cur = conn.cursor()

    conn.execute('PRAGMA foreign_keys = ON')

    @contextmanager
    def _mock_connection():
        yield conn, cur

    mocker.patch('qbitquick.database.database_handler.get_db_connection', _mock_connection)

    yield conn, cur

    cur.close()
    conn.close()