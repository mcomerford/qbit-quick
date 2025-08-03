import json
import os
import re
import threading
import uuid
from datetime import timedelta
from itertools import cycle
from pathlib import Path
from sqlite3 import Connection, Cursor
from typing import Any, Callable, Iterator
from unittest.mock import ANY, MagicMock, mock_open, patch

import pytest
from pytest import MonkeyPatch
from pytest_mock import MockerFixture
from qbittorrentapi import TorrentDictionary, TorrentInfoList, TorrentState, Tracker, TrackerStatus, TrackersList
from typer.testing import CliRunner

import qbitquick.database.database_handler
import qbitquick.handlers
import qbitquick.main
import qbitquick.task_manager
import qbitquick.utils
from conftest import initialise_mock_db
from qbitquick import cli
from qbitquick.config import TOO_MANY_REQUESTS_DELAY
from qbitquick.database.database_handler import save_torrent_hashes_to_pause
from qbitquick.log_config.logging_config import LOGGING_CONFIG
from test_helpers import assert_called_once_with_in_any_order, calculate_last_activity_time, merge_and_remove, mock_torrents_info, print_torrents


def test_default_config_is_not_created_when_no_args_are_passed_in(cli_runner: CliRunner):
    result = cli_runner.invoke(cli.app)
    assert result.exit_code == 2
    assert "Usage:" in result.stdout


def test_default_config_is_not_created_when_incomplete_args_are_passed_in(cli_runner: CliRunner):
    result = cli_runner.invoke(cli.app, ["race"])
    assert result.exit_code == 2
    assert "Missing argument 'TORRENT_HASH'" in result.stderr


def test_default_config_is_created_if_one_does_not_exist(mocker: MockerFixture, sample_config: dict[str, Any], mock_config_path: Path, cli_runner: CliRunner):
    mock_exists = mocker.patch("pathlib.Path.exists", return_value=False)
    mock_mkdir = mocker.patch("pathlib.Path.mkdir")
    with patch("builtins.open", mock_open(read_data=json.dumps(sample_config))) as mock_file:
        handle = mock_file.return_value

        result = cli_runner.invoke(cli.app, ["config", "--print"])
        assert result.exit_code == 0

        mock_exists.assert_called_once()

        mock_mkdir.assert_called_once_with(exist_ok=True, parents=True)
        handle.write.assert_called()  # Assert the default file is written

        mock_file.assert_called_with(mock_config_path / "config.json", "r")
        handle.read.assert_called()  # Assert the newly written file is read


def test_config_with_invalid_json_raises_error(mocker: MockerFixture, cli_runner: CliRunner, mock_config_path: Path):
    mocker.patch("pathlib.Path.exists", return_value=True)
    with patch("builtins.open", mock_open(read_data="{")):  # Invalid JSON
        with pytest.raises(ValueError) as excinfo:
            cli_runner.invoke(cli.app, ["config", "--print"], catch_exceptions=False)

    assert str(excinfo.value) == "Failed to load config.json: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"

@pytest.mark.parametrize(
    "override_config",
    [
        pytest.param(
            {
                "pausing": {
                    "time_active": "1z"  # Invalid pattern
                }
            },
            id="no-category"
        )
    ],
    indirect=["override_config"]
)
def test_config_with_invalid_schema_raises_error(mocker: MockerFixture, cli_runner: CliRunner, mock_config_path: Path, override_config: dict[str, Any]):
    mocker.patch("pathlib.Path.exists", return_value=True)
    with pytest.raises(ValueError) as excinfo:
        cli_runner.invoke(cli.app, ["config", "--print"], catch_exceptions=False)

    assert str(excinfo.value) == "Invalid config: '1z' is not a 'duration'"


def test_config_without_arguments(cli_runner: CliRunner):
    result = cli_runner.invoke(cli.app, ["config"])
    assert result.exit_code == 1
    assert "You must pass one of --print or --edit" in result.stdout


def test_connect_with_host_but_no_port(mocker: MockerFixture):
    config = {
        "qbittorrent": {
            "host": "localhost",
            "username": "admin",
            "password": "password"
        },
        "unrelated_key": "should_not_be_included"  # This should be filtered out
    }

    mock_client_class = mocker.patch("qbitquick.handlers.Client")
    qbitquick.handlers.connect(config)

    mock_client_class.assert_called_once_with(host="localhost", username="admin", password="password")


def test_connect_with_separate_host_and_port(mocker: MockerFixture):
    config = {
        "qbittorrent": {
            "host": "localhost",
            "port": "1234",
            "username": "admin",
            "password": "password"
        },
        "unrelated_key": "should_not_be_included"  # This should be filtered out
    }

    mock_client_class = mocker.patch("qbitquick.handlers.Client")
    qbitquick.handlers.connect(config)

    mock_client_class.assert_called_once_with(host="localhost", port="1234", username="admin", password="password")


@pytest.mark.parametrize(
    ("override_config", "category"),
    [
        pytest.param(
            {
                "racing": {
                    "race_categories": []
                },
                "debug_logging": True
            },
            "",
            id="no-category"
        ),
        pytest.param(
            {
                "racing": {
                    "race_categories": ["race"]
                },
                "debug_logging": False
            },
            "race",
            id="race-category"
        ),
    ],
    indirect=["override_config"]
)
def test_successful_race(mocker: MockerFixture, monkeypatch: MonkeyPatch, mock_client_instance: MagicMock, mock_get_db_connection: tuple[Connection, Cursor],
                         torrent_factory: Callable[..., TorrentDictionary], tracker_factory: Callable[..., Tracker], cli_runner: CliRunner,
                         override_config: dict[str, Any], category: str):
    # Setup torrents
    racing_torrent = torrent_factory(category=category, name="racing_torrent", state=TorrentState.CHECKING_DOWNLOAD)
    downloading_torrent = torrent_factory(category=category, name="downloading_torrent", state=TorrentState.DOWNLOADING, ratio=1.0, progress=0.5)
    ignored_torrent = torrent_factory(category="ignore", name="ignored_torrent", state=TorrentState.UPLOADING, ratio=1.0, progress=1.0)
    non_racing_torrent = torrent_factory(category="other", name="non_racing_torrent", state=TorrentState.DOWNLOADING, progress=0.5)
    app_paused_torrent = torrent_factory(category=category, name="app_paused_torrent", state=TorrentState.PAUSED_UPLOAD, ratio=1.0)
    manually_paused_torrent = torrent_factory(category=category, name="manually_paused_torrent", state=TorrentState.PAUSED_UPLOAD, ratio=1.0)
    uploading_torrent = torrent_factory(category=category, name="uploading_torrent", state=TorrentState.UPLOADING, ratio=2.0, progress=1.0)
    torrents = [racing_torrent, non_racing_torrent, downloading_torrent, uploading_torrent, app_paused_torrent, manually_paused_torrent, ignored_torrent]

    conn, cur = initialise_mock_db(mock_get_db_connection, downloading_torrent.hash, {app_paused_torrent.hash})

    # Setup trackers
    working_tracker = tracker_factory(status=TrackerStatus.NOT_CONTACTED, url="working_tracker")
    disabled_tracker = tracker_factory(status=TrackerStatus.DISABLED, url="disabled_tracker")
    not_working_tracker = tracker_factory(status=TrackerStatus.NOT_WORKING, url="not_working_tracker", msg="reason")
    trackers = [working_tracker, disabled_tracker, not_working_tracker]

    # Update the trackers after each loop by hooking into the sleep function
    def sleep_callback(_timeout: float | None = None, _stop_event: threading.Event | None = None, *_args: list[str] | None, **_kwargs: dict[str, Any] | None) -> bool:
        racing_torrent.state = TorrentState.DOWNLOADING
        update_tracker(working_tracker)
        return False

    mock_sleep = mocker.patch("qbitquick.handlers.interruptible_sleep", side_effect=sleep_callback)

    # Various callbacks can update the tracker status, so this list needs to match exactly how many callback there are
    tracker_statuses: Iterator[dict[str, Any]] = iter([
        # Wait called while torrent is checking, so don't change the status
        {},
        # Reannounce changes the status to updating
        {"status": TrackerStatus.UPDATING},
        # Wait after reannounce changes the status to not working
        {"status": TrackerStatus.NOT_WORKING, "msg": "unregistered"},
        # Recheck due to unregistered changes the status to updating and clears the message
        {"status": TrackerStatus.UPDATING, "msg": None},
        # Waiting while updating changes the status to not working
        {"status": TrackerStatus.NOT_WORKING, "msg": "too many requests"},
        # Waiting due to too many requests changes the status to updating and clears the message
        {"status": TrackerStatus.UPDATING, "msg": None},
        # Waiting while updating changes the status to working
        {"status": TrackerStatus.WORKING},
    ])

    def torrents_info_side_effect(**kwargs: dict[str, Any]) -> TorrentInfoList | None:
        if not kwargs:
            return TorrentInfoList(torrents)
        if kwargs.get("torrent_hashes") == racing_torrent.hash:
            return TorrentInfoList([racing_torrent])
        return None

    def torrents_trackers_side_effect(**kwargs: dict[str, Any]) -> TrackersList | None:
        if kwargs.get("torrent_hash") == racing_torrent.hash:
            return TrackersList(trackers)
        return None

    def update_tracker(tracker: Tracker) -> None:
        merge_and_remove(tracker, next(tracker_statuses))

    def torrents_pause_side_effect(**kwargs: dict[str, Any]) -> None:
        hash_to_torrent = {torrent.hash: torrent for torrent in torrents}
        for torrent_hash in kwargs["torrent_hashes"]:
            if torrent := hash_to_torrent.get(torrent_hash):
                torrent.state = TorrentState.PAUSED_DOWNLOAD

    mock_client_instance.torrents_info.side_effect = torrents_info_side_effect
    mock_client_instance.torrents_trackers.side_effect = torrents_trackers_side_effect
    mock_client_instance.torrents_reannounce.side_effect = lambda **kwargs: update_tracker(working_tracker)
    mock_client_instance.torrents_recheck.side_effect = lambda **kwargs: update_tracker(working_tracker)
    mock_client_instance.torrents_pause.side_effect = torrents_pause_side_effect

    result = cli_runner.invoke(cli.app, ["race", racing_torrent.hash])

    print_torrents(torrents)

    # Verify the script exited with a successful exit code
    assert result.exit_code == 0
    # Verify reannounce was called on the torrent, as it was in a NOT_WORKING state
    mock_client_instance.torrents_reannounce.assert_called_once_with(torrent_hashes=racing_torrent.hash)
    # Verify recheck was called on the torrent, as the tracker reported the torrent was "unregistered"
    mock_client_instance.torrents_recheck.assert_called_once_with(torrent_hashes=racing_torrent.hash)
    # Verify pause was only called on the torrents eligible for pausing
    if override_config["racing"]["race_categories"]:
        assert_called_once_with_in_any_order(mock_client_instance.torrents_pause, torrent_hashes=[app_paused_torrent.hash, non_racing_torrent.hash, uploading_torrent.hash, ])
    else:
        assert_called_once_with_in_any_order(mock_client_instance.torrents_pause, torrent_hashes=[app_paused_torrent.hash, uploading_torrent.hash, ])
    # Verify the racing torrent hash was added to the database
    cur.execute(f"SELECT id FROM pause_events WHERE id = '{racing_torrent.hash}'")
    assert cur.fetchall() == [(racing_torrent.hash,)]
    # Verify all the paused torrent hashes were added to the database
    cur.execute(f"SELECT torrent_hash FROM paused_torrents WHERE id = '{racing_torrent.hash}' ORDER BY torrent_hash")
    if override_config["racing"]["race_categories"]:
        assert cur.fetchall() == sorted([(app_paused_torrent.hash,), (non_racing_torrent.hash,), (uploading_torrent.hash,), ])
    else:
        assert cur.fetchall() == sorted([(app_paused_torrent.hash,), (uploading_torrent.hash,), ])
    # Verify the script waited for TOO_MANY_REQUESTS_DELAY seconds, as the tracker returned "too many requests"
    mock_sleep.assert_any_call(TOO_MANY_REQUESTS_DELAY, ANY)


@pytest.mark.parametrize(
    "override_config",
    [{
        "racing": {
            "race_categories": None,
            "pausing": None,
            "max_reannounce": 1
        },
    }],
    indirect=True
)
def test_racing_paused_torrent(mocker: MockerFixture, monkeypatch: MonkeyPatch, mock_client_instance: MagicMock, mock_get_db_connection: tuple[Connection, Cursor],
                               torrent_factory: Callable[..., TorrentDictionary], tracker_factory: Callable[..., Tracker], cli_runner: CliRunner,
                               override_config: dict[str, Any]):
    # Setup torrents
    racing_torrent = torrent_factory(category="race", name="racing_torrent", state=TorrentState.CHECKING_DOWNLOAD)
    downloading_torrent = torrent_factory(category="race", name="downloading_torrent", state=TorrentState.DOWNLOADING, ratio=1.0, progress=0.5)
    ignored_torrent = torrent_factory(category="ignore", name="ignored_torrent", state=TorrentState.UPLOADING, ratio=1.0, progress=1.0)
    non_racing_torrent = torrent_factory(category="other", name="non_racing_torrent", state=TorrentState.DOWNLOADING, progress=0.5)
    paused_torrent = torrent_factory(category="race", name="paused_torrent", state=TorrentState.PAUSED_DOWNLOAD, ratio=1.0, progress=0.5)
    uploading_torrent = torrent_factory(category="race", name="uploading_torrent", state=TorrentState.UPLOADING, ratio=2.0, progress=1.0)
    torrents = [racing_torrent, non_racing_torrent, downloading_torrent, uploading_torrent, paused_torrent, ignored_torrent]

    conn, cur = initialise_mock_db(mock_get_db_connection)

    # Setup trackers
    working_tracker = tracker_factory(status=TrackerStatus.NOT_CONTACTED, url="working_tracker")

    # Update the trackers after each loop by hooking into the sleep function
    def sleep_callback(_timeout: float | None = None, _stop_event: threading.Event | None = None, *_args: list[str] | None, **_kwargs: dict[str, Any] | None) -> bool:
        racing_torrent.state = TorrentState.PAUSED_DOWNLOAD
        working_tracker.update(next(tracker_statuses))
        return False

    mocker.patch("qbitquick.handlers.interruptible_sleep", side_effect=sleep_callback)

    tracker_statuses = cycle([{"status": TrackerStatus.WORKING},])

    mock_torrents_info(mock_client_instance, torrents)

    result = cli_runner.invoke(cli.app, ["race", racing_torrent.hash])

    print_torrents(torrents)

    # Verify the script exited with an unsuccessful exit code, as the racing torrent has been paused
    assert result.exit_code == 1
    # Verify nothing was paused
    mock_client_instance.torrents_pause.assert_not_called()
    # Verify the racing torrent hash was not added to the database
    cur.execute("SELECT * FROM pause_events")
    assert cur.fetchall() == []
    # Verify no paused torrents were added to the database
    cur.execute("SELECT * FROM paused_torrents")
    assert cur.fetchall() == []


@pytest.mark.parametrize("category", [None, "not_race"])
def test_racing_torrent_with_invalid_category(mock_client_instance: MagicMock, mock_get_db_connection: tuple[Connection, Cursor], torrent_factory: Callable[..., TorrentDictionary],
                                              cli_runner: CliRunner, category: str):
    # Setup racing torrent with no category
    racing_torrent = torrent_factory(category=category, name="racing_torrent", state=TorrentState.CHECKING_DOWNLOAD)

    conn, cur = initialise_mock_db(mock_get_db_connection)

    mock_client_instance.torrents_info.return_value = TorrentInfoList([racing_torrent])

    result = cli_runner.invoke(cli.app, ["race", racing_torrent.hash])

    # Verify the script exited with an unsuccessful exit code, as the racing torrent category doesn't match
    assert result.exit_code == 1
    # Verify nothing was paused
    mock_client_instance.torrents_pause.assert_not_called()
    # Verify the racing torrent hash was not added to the database
    cur.execute("SELECT * FROM pause_events")
    assert cur.fetchall() == []
    # Verify no paused torrents were added to the database
    cur.execute("SELECT * FROM paused_torrents")
    assert cur.fetchall() == []


def test_post_race(mock_client_instance: MagicMock, mock_get_db_connection: tuple[Connection, Cursor], torrent_factory: Callable[..., TorrentDictionary],
                   cli_runner: CliRunner):
    racing_torrent = torrent_factory(category="race", name="racing_torrent")
    paused_torrent = torrent_factory(category="race", name="paused_torrent", state=TorrentState.PAUSED_DOWNLOAD, ratio=1.0, progress=0.5)
    torrents = [racing_torrent, paused_torrent]

    paused_torrent_hashes = {paused_torrent.hash, "missing_torrent_hash"}
    conn, cur = initialise_mock_db(mock_get_db_connection, racing_torrent.hash, paused_torrent_hashes)

    mock_torrents_info(mock_client_instance, torrents)

    result = cli_runner.invoke(cli.app, ["post-race", racing_torrent.hash])

    # Verify the script exited with a successful exit code
    assert result.exit_code == 0
    # Verify resume was called on the paused torrents, as it was in a NOT_WORKING state
    mock_client_instance.torrents_resume.assert_called_once_with(torrent_hashes={paused_torrent.hash})
    # Verify the racing torrent hash was removed from the database
    cur.execute("SELECT * FROM pause_events")
    assert cur.fetchall() == []
    # Verify the associated paused torrent hashes were removed from the database
    cur.execute("SELECT * FROM paused_torrents")
    assert cur.fetchall() == []


def test_post_race_with_no_torrents_to_resume(mock_client_instance: MagicMock, mock_get_db_connection: tuple[Connection, Cursor], torrent_factory: Callable[..., TorrentDictionary],
                                              cli_runner: CliRunner):
    racing_torrent = torrent_factory(category="race", name="racing_torrent")

    conn, cur = initialise_mock_db(mock_get_db_connection, racing_torrent.hash, set())

    mock_client_instance.torrents_info.return_value = TorrentInfoList([racing_torrent])

    result = cli_runner.invoke(cli.app, ["post-race", racing_torrent.hash])

    # Verify the script exited with a successful exit code
    assert result.exit_code == 0
    # Verify resume was not called
    mock_client_instance.torrents_resume.assert_not_called()
    # Verify the racing torrent hash was removed from the database
    cur.execute("SELECT * FROM pause_events")
    assert cur.fetchall() == []


def test_post_race_with_unknown_hash(mock_client_instance: MagicMock, cli_runner: CliRunner):
    mock_client_instance.torrents_info.return_value = TorrentInfoList([])

    result = cli_runner.invoke(cli.app, ["post-race", "unknown_hash"])

    # Verify the script exited with an error exit code
    assert result.exit_code == 1
    # Verify resume was not called
    mock_client_instance.torrents_resume.assert_not_called()


@pytest.mark.parametrize(
    ("override_config", "with_id"),
    [
        pytest.param(
            {
                "pausing": None
            },
            False  # with_id
        ),
        pytest.param(
            {
                "pausing": {
                    "time_since_active": "1d",
                    "time_active": "1w"
                }
            },
            True,  # with_id
        )
    ],
    indirect=["override_config"]
)
def test_pause(mock_client_instance: MagicMock, mock_get_db_connection: tuple[Connection, Cursor], torrent_factory: Callable[..., TorrentDictionary],
               cli_runner: CliRunner, override_config: dict[str, Any], with_id: bool):
    # Setup torrents
    downloading_torrent = torrent_factory(category="race", name="downloading_torrent", state=TorrentState.DOWNLOADING, progress=0.5, last_activity=calculate_last_activity_time(timedelta(days=2)), time_active=timedelta(weeks=2).total_seconds())
    ignored_torrent = torrent_factory(category="ignore", name="ignored_torrent", state=TorrentState.UPLOADING, last_activity=calculate_last_activity_time(timedelta(days=2)), time_active=timedelta(weeks=2).total_seconds())
    app_paused_torrent = torrent_factory(category="race", name="app_paused_torrent", state=TorrentState.PAUSED_UPLOAD, last_activity=calculate_last_activity_time(timedelta(hours=1)), time_active=timedelta(days=3).total_seconds())
    manually_paused_torrent = torrent_factory(category="race", name="manually_paused_torrent", state=TorrentState.PAUSED_UPLOAD, last_activity=calculate_last_activity_time(timedelta(hours=1)), time_active=timedelta(days=3).total_seconds())
    new_active_uploading_torrent = torrent_factory(category="race", name="new_active_uploading_torrent", state=TorrentState.UPLOADING, last_activity=calculate_last_activity_time(timedelta(hours=1)), time_active=timedelta(days=3).total_seconds())
    old_active_uploading_torrent = torrent_factory(category="race", name="old_active_uploading_torrent", state=TorrentState.UPLOADING, last_activity=calculate_last_activity_time(timedelta(hours=1)), time_active=timedelta(weeks=1).total_seconds())
    inactive_uploading_torrent = torrent_factory(category="race", name="inactive_uploading_torrent", state=TorrentState.UPLOADING, last_activity=calculate_last_activity_time(timedelta(hours=1)), time_active=timedelta(weeks=1, days=1).total_seconds())
    torrents = [downloading_torrent, ignored_torrent, app_paused_torrent, manually_paused_torrent, new_active_uploading_torrent, old_active_uploading_torrent, inactive_uploading_torrent]

    conn, cur = initialise_mock_db(mock_get_db_connection, downloading_torrent.hash, {app_paused_torrent.hash})

    mock_torrents_info(mock_client_instance, torrents)

    if with_id:
        event_id = str(uuid.uuid4())
        result = cli_runner.invoke(cli.app, ["pause", "--id", event_id])
    else:
        event_id = "pause"
        result = cli_runner.invoke(cli.app, ["pause"])

    print_torrents(torrents)

    # Verify the script exited with a successful exit code
    assert result.exit_code == 0

    # Verify the pause event was added to the database with the specified event id
    cur.execute(f"SELECT id FROM pause_events WHERE id = '{event_id}'")
    assert cur.fetchall() == [(event_id,)]
    if "pausing" in override_config:
        # Verify only the torrents that breached the time_since_active or time_active limits were added to the database
        assert_called_once_with_in_any_order(mock_client_instance.torrents_pause, torrent_hashes=[old_active_uploading_torrent.hash, inactive_uploading_torrent.hash, ])
        cur.execute(f"SELECT torrent_hash FROM paused_torrents WHERE id = '{event_id}' ORDER BY torrent_hash")
        assert cur.fetchall() == sorted([(old_active_uploading_torrent.hash,), (inactive_uploading_torrent.hash,), ])
    else:
        # Verify all eligible paused torrent hashes were added to the database
        assert_called_once_with_in_any_order(mock_client_instance.torrents_pause, torrent_hashes=[
            app_paused_torrent.hash, new_active_uploading_torrent.hash, old_active_uploading_torrent.hash, inactive_uploading_torrent.hash, ])
        cur.execute(f"SELECT torrent_hash FROM paused_torrents WHERE id = '{event_id}' ORDER BY torrent_hash")
        assert cur.fetchall() == sorted([(app_paused_torrent.hash,), (new_active_uploading_torrent.hash,),
                                         (old_active_uploading_torrent.hash,), (inactive_uploading_torrent.hash,), ])


@pytest.mark.parametrize("with_id", [False, True])
def test_unpause(mock_client_instance: MagicMock, mock_get_db_connection: tuple[Connection, Cursor], torrent_factory: Callable[..., TorrentDictionary],
                 cli_runner: CliRunner, with_id: bool):
    # Setup torrents
    racing_torrent = torrent_factory(category="race", name="racing_torrent", state=TorrentState.DOWNLOADING)
    paused_torrent = torrent_factory(category="race", name="paused_torrent", state=TorrentState.PAUSED_UPLOAD)
    other_paused_torrent = torrent_factory(category="other", name="paused_torrent", state=TorrentState.PAUSED_UPLOAD)
    ignored_torrent = torrent_factory(category="ignore", name="paused_torrent", state=TorrentState.PAUSED_UPLOAD)
    torrents = [racing_torrent, paused_torrent, other_paused_torrent, ignored_torrent]

    paused_torrents = {paused_torrent.hash, other_paused_torrent.hash}
    conn, cur = initialise_mock_db(mock_get_db_connection, racing_torrent._torrent_hash, {paused_torrent.hash})

    mock_torrents_info(mock_client_instance, torrents)

    if with_id:
        event_id = str(uuid.uuid4())
        save_torrent_hashes_to_pause(event_id, paused_torrents)
        result = cli_runner.invoke(cli.app, ["unpause", "--id", event_id])
    else:
        event_id = "pause"
        save_torrent_hashes_to_pause(event_id, paused_torrents)
        result = cli_runner.invoke(cli.app, ["unpause"])

    print_torrents(torrents)

    # Verify the script exited with a successful exit code
    assert result.exit_code == 0
    # Verify resume was only called on other_paused_torrent, as paused_torrent is still paused due to racing_torrent
    mock_client_instance.torrents_resume.assert_called_once_with(torrent_hashes={other_paused_torrent.hash})
    # Verify the pause event was removed from the database
    cur.execute(f"SELECT * FROM pause_events WHERE id = '{event_id}'")
    assert cur.fetchall() == []
    # Verify the associated paused torrent hashes were removed from the database
    cur.execute(f"SELECT * FROM paused_torrents WHERE id = '{event_id}'")
    assert cur.fetchall() == []


def test_db_without_arguments(mock_get_db_connection: tuple[Connection, Cursor], torrent_factory: Callable[..., TorrentDictionary], cli_runner: CliRunner):
    result = cli_runner.invoke(cli.app, ["db"])
    assert result.exit_code == 1
    assert "You must pass one of --print, --clear, or --delete" in result.stdout


def test_print_db(mock_get_db_connection: tuple[Connection, Cursor], torrent_factory: Callable[..., TorrentDictionary], cli_runner: CliRunner):
    racing_torrent = torrent_factory()
    paused_torrent1 = torrent_factory()
    paused_torrent2 = torrent_factory()

    paused_torrent_hashes = {paused_torrent1.hash, paused_torrent2.hash}
    initialise_mock_db(mock_get_db_connection, racing_torrent.hash, paused_torrent_hashes)

    result = cli_runner.invoke(cli.app, ["db", "--print"])
    assert result.exit_code == 0

    joined_hashes = ".*".join(sorted(paused_torrent_hashes))
    assert re.search(fr"{racing_torrent.hash}.*{joined_hashes}", result.stdout, flags=re.DOTALL)


def test_clear_db_clears_the_db_if_input_is_y(monkeypatch: MonkeyPatch, mock_get_db_connection: tuple[Connection, Cursor], torrent_factory: Callable[..., TorrentDictionary],
                                              cli_runner: CliRunner):
    monkeypatch.setattr("builtins.input", lambda _: "y")

    racing_torrent = torrent_factory()
    paused_torrent1 = torrent_factory()
    paused_torrent2 = torrent_factory()

    conn, cur = initialise_mock_db(mock_get_db_connection, racing_torrent.hash, {paused_torrent1.hash, paused_torrent2.hash})

    result = cli_runner.invoke(cli.app, ["db", "--clear"])
    assert result.exit_code == 0

    # Verify all pause events were removed from the database
    cur.execute("SELECT * FROM pause_events")
    assert cur.fetchall() == []
    # Verify all paused torrent hashes were removed from the database
    cur.execute("SELECT * FROM paused_torrents")
    assert cur.fetchall() == []


def test_clear_db_does_nothing_if_input_is_not_y(monkeypatch: MonkeyPatch, mock_get_db_connection: tuple[Connection, Cursor], torrent_factory: Callable[..., TorrentDictionary],
                                                 cli_runner: CliRunner):
    monkeypatch.setattr("builtins.input", lambda _: "n")

    racing_torrent = torrent_factory()
    paused_torrent1 = torrent_factory()
    paused_torrent2 = torrent_factory()

    conn, cur = initialise_mock_db(mock_get_db_connection, racing_torrent.hash, {paused_torrent1.hash, paused_torrent2.hash})

    result = cli_runner.invoke(cli.app, ["db", "--clear"])
    assert result.exit_code == 0

    # Verify the racing torrent pause event was not removed from the database
    cur.execute("SELECT id FROM pause_events")
    assert cur.fetchall() == [(racing_torrent.hash,)]
    # Verify the associated paused torrent hashes were not removed from the database
    cur.execute("SELECT torrent_hash FROM paused_torrents ORDER BY torrent_hash")
    assert cur.fetchall() == sorted([(paused_torrent1.hash,), (paused_torrent2.hash,), ])


def test_delete_entry_from_db(monkeypatch: MonkeyPatch, mock_get_db_connection: tuple[Connection, Cursor], torrent_factory: Callable[..., TorrentDictionary],
                              cli_runner: CliRunner):
    racing_torrent1 = torrent_factory()
    racing_torrent2 = torrent_factory()
    paused_torrent1 = torrent_factory()
    paused_torrent2 = torrent_factory()
    paused_torrent3 = torrent_factory()

    conn, cur = initialise_mock_db(mock_get_db_connection, racing_torrent1.hash, {paused_torrent1.hash, paused_torrent2.hash})
    save_torrent_hashes_to_pause(racing_torrent2.hash, {paused_torrent2.hash, paused_torrent3.hash})

    result = cli_runner.invoke(cli.app, ["db", "--delete", racing_torrent1.hash])
    assert result.exit_code == 0
    assert "1 row deleted" in result.stdout

    # Verify only racing_torrent2 remains in the database
    cur.execute("SELECT id FROM pause_events")
    assert cur.fetchall() == [(racing_torrent2.hash,)]
    # Verify only paused_torrent2 and paused_torrent3 remain in the database
    cur.execute("SELECT torrent_hash FROM paused_torrents ORDER BY torrent_hash")
    assert cur.fetchall() == sorted([(paused_torrent2.hash,), (paused_torrent3.hash,), ])


def test_start_server(mocker: MockerFixture, sample_config: dict[str, Any], cli_runner: CliRunner):
    mock_app = mocker.Mock()
    mock_create_app = mocker.patch("qbitquick.cli.create_app", return_value=mock_app)
    mock_is_port_in_use = mocker.patch("qbitquick.handlers.is_port_in_use", return_value=False)
    mock_run = mocker.patch("qbitquick.handlers.uvicorn.run")

    result = cli_runner.invoke(cli.app, ["server"])

    mock_create_app.assert_called_once()
    mock_is_port_in_use.assert_called_once_with(8081)
    mock_run.assert_called_once_with(mock_app, host="0.0.0.0", port=8081, log_config=LOGGING_CONFIG)

    # Verify the script exited with a successful exit code
    assert result.exit_code == 0


# noinspection PyShadowingBuiltins
@pytest.mark.parametrize(
    ("status", "include_field_names", "fields", "format"),
    [
        pytest.param(
            "all",
            True,
            None,  # None means include all fields
            "json"
        ),
        pytest.param(
            "paused",
            False,
            ["name", "state,ratio"],
            "json"
        ),
        pytest.param(
            "all",
            True,
            None,  # None means include all fields
            "plain"
        ),
        pytest.param(
            "paused",
            False,
            ["name", "state,ratio"],
            "plain"
        ),
    ]
)
def test_info(mock_client_instance: MagicMock, mock_get_db_connection: tuple[Connection, Cursor], torrent_factory: Callable[..., TorrentDictionary],
              cli_runner: CliRunner, status: str, include_field_names: bool, fields: list[str] | None, format: str):
    # Setup torrents
    racing_torrent = torrent_factory(name="racing_torrent", state=TorrentState.CHECKING_DOWNLOAD)
    downloading_torrent = torrent_factory(name="downloading_torrent", state=TorrentState.DOWNLOADING, ratio=1.0, progress=0.5)
    ignored_torrent = torrent_factory(name="ignored_torrent", state=TorrentState.UPLOADING, ratio=1.0, progress=1.0)
    non_racing_torrent = torrent_factory(name="non_racing_torrent", state=TorrentState.DOWNLOADING, progress=0.5)
    app_paused_torrent = torrent_factory(name="app_paused_torrent", state=TorrentState.PAUSED_UPLOAD, ratio=1.0)
    manually_paused_torrent = torrent_factory(name="manually_paused_torrent", state=TorrentState.PAUSED_UPLOAD, ratio=1.0)
    uploading_torrent = torrent_factory(name="uploading_torrent", state=TorrentState.UPLOADING, ratio=2.0, progress=1.0)
    torrents = [racing_torrent, non_racing_torrent, downloading_torrent, uploading_torrent, app_paused_torrent, manually_paused_torrent, ignored_torrent]

    # noinspection PyShadowingNames
    def torrents_info_side_effect(*args: list[str], **_kwargs: dict[str, Any]) -> TorrentInfoList | None:
        if args:
            match args[0]:
                case "paused":
                    return TorrentInfoList([t for t in torrents if t.state_enum.is_paused])
        return TorrentInfoList(torrents)

    mock_client_instance.torrents_info.side_effect = torrents_info_side_effect

    args = ["info", "--status", status, "--format", format]
    if include_field_names:
        args.append("--include-field-names")
    if fields:
        for field in fields:
            args.extend(["-f", field])
    result = cli_runner.invoke(cli.app, args)

    print_torrents(torrents)

    # Verify the script exited with a successful exit code
    assert result.exit_code == 0

    if format == "json":
        if include_field_names:
            assert result.stdout.rstrip() == json.dumps(torrents, indent=2)
        else:
            expected = [
                [app_paused_torrent["name"], app_paused_torrent["state"], app_paused_torrent["ratio"]],
                [manually_paused_torrent["name"], manually_paused_torrent["state"], manually_paused_torrent["ratio"]]
            ]
            assert result.stdout.rstrip() == json.dumps(expected, indent=2)
    elif format == "plain":
        if include_field_names:
            assert result.stdout.rstrip() == (
                    "category,hash,name,progress,ratio,state" + os.linesep +
                    f"race,{racing_torrent.hash},racing_torrent,0,0,TorrentState.CHECKING_DOWNLOAD" + os.linesep +
                    f"race,{non_racing_torrent.hash},non_racing_torrent,0.5,0,TorrentState.DOWNLOADING" + os.linesep +
                    f"race,{downloading_torrent.hash},downloading_torrent,0.5,1.0,TorrentState.DOWNLOADING" + os.linesep +
                    f"race,{uploading_torrent.hash},uploading_torrent,1.0,2.0,TorrentState.UPLOADING" + os.linesep +
                    f"race,{app_paused_torrent.hash},app_paused_torrent,0,1.0,TorrentState.PAUSED_UPLOAD" + os.linesep +
                    f"race,{manually_paused_torrent.hash},manually_paused_torrent,0,1.0,TorrentState.PAUSED_UPLOAD" + os.linesep +
                    f"race,{ignored_torrent.hash},ignored_torrent,1.0,1.0,TorrentState.UPLOADING"
            )
        else:
            assert result.stdout.rstrip() == (
                    "app_paused_torrent,TorrentState.PAUSED_UPLOAD,1.0" + os.linesep +
                    "manually_paused_torrent,TorrentState.PAUSED_UPLOAD,1.0"
            )


def test_edit_config_uses_editor_env(cli_runner: CliRunner, mock_config_path: Path):
    with patch.dict(os.environ, {"EDITOR": "nano"}), patch("subprocess.run") as mock_run:
        result = cli_runner.invoke(cli.app, ["config", "--edit"])

    assert result.exit_code == 0
    mock_run.assert_called_once_with(["nano", str(mock_config_path / "config.json")])


def test_edit_config_falls_back_to_vi_on_unix(cli_runner: CliRunner, mock_config_path: Path):
    with patch.dict(os.environ, {}, clear=True), patch("subprocess.run") as mock_run, patch("sys.platform", "linux"):
        result = cli_runner.invoke(cli.app, ["config", "--edit"])

    assert result.exit_code == 0
    mock_run.assert_called_once_with(["vi", str(mock_config_path / "config.json")])


def test_edit_config_falls_back_to_notepad_on_windows(cli_runner: CliRunner, mock_config_path: Path):
    with patch.dict(os.environ, {}, clear=True), patch("subprocess.run") as mock_run, patch("sys.platform", "win32"):
        result = cli_runner.invoke(cli.app, ["config", "--edit"])

    assert result.exit_code == 0
    mock_run.assert_called_once_with(["notepad", str(mock_config_path / "config.json")])


def test_edit_config_raises_error_on_failure(cli_runner: CliRunner, mock_config_path: Path):
    with patch("subprocess.run", side_effect=OSError("boom")):
        with pytest.raises(IOError) as excinfo:
            cli_runner.invoke(cli.app, ["config", "--edit"], catch_exceptions=False)

    assert str(excinfo.value) == f"Could not open file: {mock_config_path / "config.json"}"
