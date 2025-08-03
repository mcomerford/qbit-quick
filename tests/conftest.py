import json
import logging.config
import sqlite3
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from random import getrandbits
from sqlite3 import Connection, Cursor
from typing import Any, Callable, Generator
from unittest.mock import MagicMock, mock_open

import pytest
from fastapi.testclient import TestClient
from pytest import FixtureRequest
from pytest import MonkeyPatch
from pytest_mock import MockerFixture
from qbittorrentapi import Client, TorrentDictionary, TorrentState, Tracker, TrackerStatus
from typer.testing import CliRunner

from qbitquick.log_config import logging_config
from qbitquick.server import create_app
from test_helpers import merge_and_remove


@pytest.fixture(autouse=True)
def override_logging_config(monkeypatch: MonkeyPatch) -> None:
    test_logging_config = {
        "version": 1,
        "formatters": {
            "default": {
                "format": "%(asctime)s.%(msecs)03d %(thread)d [%(levelname)s] %(message)s",
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "level": "DEBUG",
                "stream": "ext://sys.stdout",
            }
        },
        "root": {
            "handlers": ["console"],
            "level": "DEBUG",
        },
        "disable_existing_loggers": False,
    }

    monkeypatch.setattr(logging_config, "LOGGING_CONFIG", test_logging_config)
    logging.config.dictConfig(test_logging_config)


@pytest.fixture
def sample_config() -> dict[str, Any]:
    return {
        "qbittorrent": {
            "host": "localhost",
            "port": 1234,
            "username": "admin",
            "password": "password"
        },
        "ignore_categories": ["ignore"],
        "racing": {
            "race_categories": ["race"],
            "pausing": {
                "ratio": 1.0
            }
        },
        "pausing": {
            "time_since_active": "1d",
            "time_active": "1w"
        }
    }


@pytest.fixture(autouse=True)
def override_config(monkeypatch: MonkeyPatch, sample_config: dict[str, Any], request: FixtureRequest) -> dict[str, Any]:
    """Fixture that merges sample_config with test-specific updates."""
    config_updates = getattr(request, "param", {})  # Get param from parametrize
    merge_and_remove(sample_config, config_updates)

    mock_file_content = json.dumps(sample_config)
    mocked_open = mock_open(read_data=mock_file_content)
    monkeypatch.setattr("builtins.open", mocked_open)

    return sample_config


@pytest.fixture
def mock_config_path(mocker: MockerFixture) -> Path:
    mock = mocker.patch("qbitquick.config.platformdirs.user_config_dir", return_value=Path("mock/config"))
    return mock.return_value


@pytest.fixture
def mock_client_instance(mocker: MockerFixture) -> MagicMock:
    mock = mocker.patch("qbitquick.handlers.Client", autospec=True)
    mocked_client_instance = mock.return_value
    mock_build_info = mocker.MagicMock()
    mock_build_info.items.return_value = [("version", "1.2.3")]
    mocked_client_instance.app.build_info = mock_build_info
    return mocked_client_instance


@pytest.fixture
def mock_get_db_connection(mocker: MockerFixture) -> Generator[tuple[Connection, Cursor], None, None]:
    """Mock `get_db_connection` to return an in-memory SQLite database."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    cur = conn.cursor()

    conn.execute("PRAGMA foreign_keys = ON")

    @contextmanager
    def _mock_connection():
        yield conn, cur

    mocker.patch("qbitquick.database.database_handler.get_db_connection", _mock_connection)

    yield conn, cur

    cur.close()
    conn.close()


@pytest.fixture
def test_server() -> Generator[TestClient, None, None]:
    app = create_app()
    with TestClient(app) as client:
        yield client


@pytest.fixture
def torrent_factory(mock_client_instance: Client) -> Callable[..., TorrentDictionary]:
    def _create_torrent(**kwargs):
        default_values = {
            "category": "race",
            "hash": f"%032x" % getrandbits(160),
            "name": "torrent_name",
            "progress": 0,
            "ratio": 0,
            "state": TorrentState.UNKNOWN,
        }
        default_values.update(kwargs)
        return TorrentDictionary(client=mock_client_instance, data=default_values)

    return _create_torrent


@pytest.fixture
def tracker_factory() -> Callable[..., Tracker]:
    def _create_tracker(**kwargs):
        default_values = {
            "msg": "",
            "status": TrackerStatus.DISABLED,
            "url": "",
        }
        default_values.update(kwargs)
        return Tracker(default_values)

    return _create_tracker


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


def initialise_mock_db(mock_get_db_connection: tuple[Connection, Cursor], event_id: str | None = None, paused_torrent_hashes: set[str] | None = None) -> tuple[Connection, Cursor]:
    """Create a connection to the in-memory database, create the tables and preload them with the provided data"""
    if paused_torrent_hashes is None:
        paused_torrent_hashes = set()
    conn, cur = mock_get_db_connection

    ddl_file = resources.files("qbitquick") / "resources" / "pause_events.ddl"
    with ddl_file.open("r") as f:
        cur.executescript(f.read())
    if event_id and paused_torrent_hashes:
        cur.execute("BEGIN TRANSACTION")
        cur.execute("""
            INSERT INTO pause_events (id)
            VALUES (?)
        """, (event_id,))
        cur.executemany("""
            INSERT INTO paused_torrents (id, torrent_hash)
            VALUES (?, ?)
        """, [(event_id, paused_hash,) for paused_hash in paused_torrent_hashes])
        conn.commit()

    return conn, cur