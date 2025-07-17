import contextlib
import json
import re
import threading
from io import StringIO
from itertools import cycle
from pathlib import Path
from sqlite3 import Connection, Cursor
from typing import Any, Callable, Iterator
from unittest.mock import ANY, MagicMock, mock_open, patch

import pytest
from pytest import MonkeyPatch
from pytest_mock import MockerFixture
from qbittorrentapi import TorrentDictionary, TorrentInfoList, TorrentState, Tracker, TrackerStatus, TrackersList

import qbitquick.database.database_handler
import qbitquick.handlers
import qbitquick.main
import qbitquick.task_manager
import qbitquick.utils
from conftest import initialise_mock_db
from qbitquick.config import TOO_MANY_REQUESTS_DELAY
from qbitquick.log_config.logging_config import LOGGING_CONFIG
from test_helpers import assert_called_once_with_in_any_order, merge_and_remove


def test_default_config_is_not_created_when_no_args_are_passed_in(run_main: Callable[..., int]):
    with contextlib.redirect_stdout(StringIO()) as temp_stdout:
        exit_code = run_main()

        assert exit_code == 0
        assert temp_stdout.getvalue().startswith("usage:")


def test_default_config_is_not_created_when_incomplete_args_are_passed_in(run_main: Callable[..., int]):
    with contextlib.redirect_stderr(StringIO()) as temp_stderr:
        exit_code = run_main("race")

        assert exit_code == 2
        assert "the following arguments are required: torrent_hash" in temp_stderr.getvalue()


def test_default_config_is_created_if_one_does_not_exist(mocker: MockerFixture, sample_config: dict[str, Any], mock_config_path: Path, run_main: Callable[..., int]):
    mock_exists = mocker.patch("pathlib.Path.exists", return_value=False)
    mock_mkdir = mocker.patch("pathlib.Path.mkdir")
    with patch("builtins.open", mock_open(read_data=json.dumps(sample_config))) as mock_file:
        handle = mock_file.return_value

        run_main("config", "--print")

        mock_exists.assert_called_once()

        mock_mkdir.assert_called_once_with(exist_ok=True, parents=True)
        handle.write.assert_called()  # Assert the default file is written

        mock_file.assert_called_with(mock_config_path / "config.json", "r")
        handle.read.assert_called()  # Assert the newly written file is read


def test_connect_with_host_but_no_port(mocker: MockerFixture):
    config = {
        "host": "localhost",
        "username": "admin",
        "password": "password",
        "unrelated_key": "should_not_be_included"  # This should be filtered out
    }

    mock_client_class = mocker.patch("qbitquick.handlers.Client")
    qbitquick.handlers.connect(config)

    mock_client_class.assert_called_once_with(host="localhost", username="admin", password="password")


def test_connect_with_separate_host_and_port(mocker: MockerFixture):
    config = {
        "host": "localhost",
        "port": "1234",
        "username": "admin",
        "password": "password",
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
                "race_categories": [],
                "debug_logging": True
            },
            "",
            id="no-category"
        ),
        pytest.param(
            {
                "race_categories": ["race"],
                "debug_logging": False
            },
            "race",
            id="race-category"
        ),
    ],
    indirect=["override_config"]
)
def test_successful_race(mocker: MockerFixture, monkeypatch: MonkeyPatch, mock_client_instance: MagicMock, mock_get_db_connection: tuple[Connection, Cursor],
                         torrent_factory: Callable[..., TorrentDictionary], tracker_factory: Callable[..., Tracker], run_main: Callable[..., int],
                         override_config: dict[str, Any], category: str):
    # Setup torrents
    racing_torrent = torrent_factory(category=category, name="racing_torrent", state=TorrentState.CHECKING_DOWNLOAD)
    downloading_torrent = torrent_factory(category=category, name="downloading_torrent", state=TorrentState.DOWNLOADING, ratio=1.0, progress=0.5)
    ignored_torrent = torrent_factory(category="ignore", name="ignored_torrent", state=TorrentState.UPLOADING, ratio=1.0, progress=1.0)
    non_racing_torrent = torrent_factory(category="other", name="non_racing_torrent", state=TorrentState.DOWNLOADING, progress=0.5)
    paused_torrent = torrent_factory(category=category, name="paused_torrent", state=TorrentState.PAUSED_DOWNLOAD, ratio=1.0, progress=0.5)
    uploading_torrent = torrent_factory(category=category, name="uploading_torrent", state=TorrentState.UPLOADING, ratio=2.0, progress=1.0)
    torrents = [racing_torrent, non_racing_torrent, downloading_torrent, uploading_torrent, paused_torrent, ignored_torrent]

    conn, cur = initialise_mock_db(mock_get_db_connection)

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

    exit_code = run_main("race", racing_torrent.hash)

    # Print out the torrents, so we have all the information if an assertion fails
    print(json.dumps(torrents))

    # Verify the script exited with a successful exit code
    assert exit_code == 0
    # Verify reannounce was called on the torrent, as it was in a NOT_WORKING state
    mock_client_instance.torrents_reannounce.assert_called_once_with(torrent_hashes=racing_torrent.hash)
    # Verify recheck was called on the torrent, as the tracker reported the torrent was "unregistered"
    mock_client_instance.torrents_recheck.assert_called_once_with(torrent_hashes=racing_torrent.hash)
    # Verify pause was only called on the torrents eligible for pausing
    assert_called_once_with_in_any_order(mock_client_instance.torrents_pause, torrent_hashes=[downloading_torrent.hash, non_racing_torrent.hash, uploading_torrent.hash, ])
    # Verify the racing torrent hash was added to the database
    cur.execute("SELECT racing_torrent_hash FROM racing_torrents")
    assert cur.fetchall() == [(racing_torrent.hash,)]
    # Verify all the paused torrent hashes were added to the database
    cur.execute("SELECT paused_torrent_hash FROM paused_torrents ORDER BY paused_torrent_hash")
    assert cur.fetchall() == sorted([(downloading_torrent.hash,), (non_racing_torrent.hash,), (uploading_torrent.hash,), ])
    # Verify the script waited for TOO_MANY_REQUESTS_DELAY seconds, as the tracker returned "too many requests"
    mock_sleep.assert_any_call(TOO_MANY_REQUESTS_DELAY, ANY)


@pytest.mark.parametrize(
    "override_config",
    [{
        "race_categories": None,
        "ignore_categories": None,
        "pausing": False,
        "max_reannounce": 1
    }],
    indirect=True
)
def test_racing_paused_torrent(mocker: MockerFixture, monkeypatch: MonkeyPatch, mock_client_instance: MagicMock, mock_get_db_connection: tuple[Connection, Cursor],
                               torrent_factory: Callable[..., TorrentDictionary], tracker_factory: Callable[..., Tracker], run_main: Callable[..., int],
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

    def torrents_info_side_effect(**kwargs: dict[str, Any]) -> TorrentInfoList | None:
        if not kwargs:
            return TorrentInfoList(torrents)
        if kwargs.get("torrent_hashes") == racing_torrent.hash:
            return TorrentInfoList([racing_torrent])
        return None

    mock_client_instance.torrents_info.side_effect = torrents_info_side_effect

    exit_code = run_main("race", racing_torrent.hash)

    # Print out the torrents, so we have all the information if an assertion fails
    print(json.dumps(torrents))

    # Verify the script exited with an unsuccessful exit code, as the racing torrent has been paused
    assert exit_code == 1
    # Verify nothing was paused
    mock_client_instance.torrents_pause.assert_not_called()
    # Verify the racing torrent hash was not added to the database
    cur.execute("SELECT COUNT(*) FROM racing_torrents")
    assert cur.fetchone() == (0,)
    # Verify no paused torrents were added to the database
    cur.execute("SELECT COUNT(*) FROM paused_torrents")
    assert cur.fetchone() == (0,)


@pytest.mark.parametrize("category", [None, "not_race"])
def test_racing_torrent_with_invalid_category(mock_client_instance: MagicMock, mock_get_db_connection: tuple[Connection, Cursor], torrent_factory: Callable[..., TorrentDictionary],
                                              run_main: Callable[..., int], category: str):
    # Setup racing torrent with no category
    racing_torrent = torrent_factory(category=category, name="racing_torrent", state=TorrentState.CHECKING_DOWNLOAD)

    conn, cur = initialise_mock_db(mock_get_db_connection)

    mock_client_instance.torrents_info.return_value = TorrentInfoList([racing_torrent])

    exit_code = run_main("race", racing_torrent.hash)

    # Verify the script exited with an unsuccessful exit code, as the racing torrent category doesn't match
    assert exit_code == 1
    # Verify nothing was paused
    mock_client_instance.torrents_pause.assert_not_called()
    # Verify the racing torrent hash was not added to the database
    cur.execute("SELECT COUNT(*) FROM racing_torrents")
    assert cur.fetchone() == (0,)
    # Verify no paused torrents were added to the database
    cur.execute("SELECT COUNT(*) FROM paused_torrents")
    assert cur.fetchone() == (0,)


def test_post_race(mock_client_instance: MagicMock, mock_get_db_connection: tuple[Connection, Cursor], torrent_factory: Callable[..., TorrentDictionary],
                   run_main: Callable[..., int]):
    racing_torrent = torrent_factory(category="race", name="racing_torrent")
    paused_torrent = torrent_factory(category="race", name="paused_torrent", state=TorrentState.PAUSED_DOWNLOAD, ratio=1.0, progress=0.5)
    torrents = [racing_torrent, paused_torrent]

    paused_torrent_hashes = [paused_torrent.hash, "missing_torrent_hash"]
    conn, cur = initialise_mock_db(mock_get_db_connection, racing_torrent.hash, paused_torrent_hashes)

    def torrents_info_side_effect(**kwargs: dict[str, Any]) -> TorrentInfoList | None:
        return TorrentInfoList([t for t in torrents if t.hash in kwargs["torrent_hashes"]])

    mock_client_instance.torrents_info.side_effect = torrents_info_side_effect

    exit_code = run_main("post-race", racing_torrent.hash)

    # Verify the script exited with a successful exit code
    assert exit_code == 0
    # Verify resume was called on the paused torrents, as it was in a NOT_WORKING state
    mock_client_instance.torrents_resume.assert_called_once_with(torrent_hashes={paused_torrent.hash})
    # Verify the racing torrent hash was removed from the database
    cur.execute("SELECT * FROM racing_torrents")
    assert cur.fetchall() == []
    # Verify the associated paused torrent hashes was removed from the database
    cur.execute("SELECT * FROM paused_torrents")
    assert cur.fetchall() == []


def test_post_race_with_no_torrents_to_resume(mock_client_instance: MagicMock, mock_get_db_connection: tuple[Connection, Cursor], torrent_factory: Callable[..., TorrentDictionary],
                                              run_main: Callable[..., int]):
    racing_torrent = torrent_factory(category="race", name="racing_torrent")

    conn, cur = initialise_mock_db(mock_get_db_connection, racing_torrent.hash, [])

    mock_client_instance.torrents_info.return_value = TorrentInfoList([racing_torrent])

    exit_code = run_main("post-race", racing_torrent.hash)

    # Verify the script exited with a successful exit code
    assert exit_code == 0
    # Verify resume was not called
    mock_client_instance.torrents_resume.assert_not_called()
    # Verify the racing torrent hash was removed from the database
    cur.execute("SELECT * FROM racing_torrents")
    assert cur.fetchall() == []


def test_post_race_with_unknown_hash(mock_client_instance: MagicMock, run_main: Callable[..., int]):
    mock_client_instance.torrents_info.return_value = TorrentInfoList([])

    exit_code = run_main("post-race", "unknown_hash")

    # Verify the script exited with an error exit code
    assert exit_code == 1
    # Verify resume was not called
    mock_client_instance.torrents_resume.assert_not_called()


def test_print_db(mock_get_db_connection: tuple[Connection, Cursor], torrent_factory: Callable[..., TorrentDictionary], run_main: Callable[..., int]):
    racing_torrent = torrent_factory()
    paused_torrent1 = torrent_factory()
    paused_torrent2 = torrent_factory()

    paused_torrent_hashes = [paused_torrent1.hash, paused_torrent2.hash]
    initialise_mock_db(mock_get_db_connection, racing_torrent.hash, paused_torrent_hashes)

    with contextlib.redirect_stdout(StringIO()) as temp_stdout:
        run_main("db", "--print")
        joined_hashes = ".*".join(sorted(paused_torrent_hashes))
        assert re.search(fr"{racing_torrent.hash}.*{joined_hashes}", temp_stdout.getvalue(), flags=re.DOTALL)


def test_clear_db_clears_the_db_if_input_is_y(monkeypatch: MonkeyPatch, mock_get_db_connection: tuple[Connection, Cursor], torrent_factory: Callable[..., TorrentDictionary],
                                              run_main: Callable[..., int]):
    monkeypatch.setattr("builtins.input", lambda _: "y")

    racing_torrent = torrent_factory()
    paused_torrent1 = torrent_factory()
    paused_torrent2 = torrent_factory()

    conn, cur = initialise_mock_db(mock_get_db_connection, racing_torrent.hash, [paused_torrent1.hash, paused_torrent2.hash])

    run_main("db", "--clear")

    cur.execute("SELECT * FROM racing_torrents")
    assert cur.fetchall() == []
    cur.execute("SELECT * FROM paused_torrents")
    assert cur.fetchall() == []


def test_clear_db_does_nothing_if_input_is_not_y(monkeypatch: MonkeyPatch, mock_get_db_connection: tuple[Connection, Cursor], torrent_factory: Callable[..., TorrentDictionary],
                                                 run_main: Callable[..., int]):
    monkeypatch.setattr("builtins.input", lambda _: "n")

    racing_torrent = torrent_factory()
    paused_torrent1 = torrent_factory()
    paused_torrent2 = torrent_factory()

    conn, cur = initialise_mock_db(mock_get_db_connection, racing_torrent.hash, [paused_torrent1.hash, paused_torrent2.hash])

    run_main("db", "--clear")

    cur.execute("SELECT racing_torrent_hash FROM racing_torrents")
    assert cur.fetchall() == [(racing_torrent.hash,)]
    cur.execute("SELECT paused_torrent_hash FROM paused_torrents ORDER BY paused_torrent_hash")
    assert cur.fetchall() == sorted([(paused_torrent1.hash,), (paused_torrent2.hash,), ])


def test_delete_entry_from_db(monkeypatch: MonkeyPatch, mock_get_db_connection: tuple[Connection, Cursor], torrent_factory: Callable[..., TorrentDictionary],
                              run_main: Callable[..., int]):
    racing_torrent1 = torrent_factory()
    racing_torrent2 = torrent_factory()
    paused_torrent1 = torrent_factory()
    paused_torrent2 = torrent_factory()

    conn, cur = initialise_mock_db(mock_get_db_connection, racing_torrent1.hash, [paused_torrent1.hash, paused_torrent2.hash])
    cur.execute("BEGIN TRANSACTION")
    cur.execute("""
                INSERT INTO racing_torrents (racing_torrent_hash)
                VALUES (?)
                """, (racing_torrent2.hash,))
    cur.executemany("""
                    INSERT INTO paused_torrents (racing_torrent_hash, paused_torrent_hash)
                    VALUES (?, ?)
                    """, [(racing_torrent2.hash, paused_torrent1.hash), (racing_torrent2.hash, paused_torrent2.hash)])
    conn.commit()

    rows_deleted = run_main("db", "--delete", racing_torrent1.hash)
    assert rows_deleted == 1

    cur.execute("SELECT racing_torrent_hash FROM racing_torrents")
    assert cur.fetchall() == [(racing_torrent2.hash,)]
    cur.execute("SELECT paused_torrent_hash FROM paused_torrents ORDER BY paused_torrent_hash")
    assert cur.fetchall() == sorted([(paused_torrent1.hash,), (paused_torrent2.hash,), ])


def test_start_server(mocker: MockerFixture, sample_config: dict[str, Any], run_main: Callable[..., int]):
    mock_app = mocker.Mock()
    mock_create_app = mocker.patch("qbitquick.main.create_app", return_value=mock_app)
    mock_run = mocker.patch("qbitquick.handlers.uvicorn.run")

    exit_code = run_main("server")

    mock_create_app.assert_called_once()
    mock_run.assert_called_once_with(mock_app, host="0.0.0.0", port=8081, log_config=LOGGING_CONFIG)
    # assert_called_once_with_in_any_order(mock_run, mock_app, host="0.0.0.0", port=8081, log_config=sample_config)

    # Verify the script exited with a successful exit code
    assert exit_code == 0