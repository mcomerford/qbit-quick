import contextlib
import json
import os.path
import re
import time
from importlib import resources
from io import StringIO
from itertools import cycle
from unittest.mock import patch, mock_open

import pytest
from qbittorrentapi import TorrentInfoList, TorrentState, TrackerStatus, TrackersList

import qbitquick.database.database_handler
import qbitquick.qbit_quick
from qbitquick.config import TOO_MANY_REQUESTS_DELAY
from test_helpers import assert_called_once_with_in_any_order, merge_and_remove


def test_default_config_is_not_created_when_no_args_are_passed_in(monkeypatch):
    monkeypatch.setattr("sys.argv", [""])
    with contextlib.redirect_stdout(StringIO()) as temp_stdout:
        with pytest.raises(SystemExit) as cm:
            qbitquick.qbit_quick.main()

        assert cm.value.code == 0
        assert temp_stdout.getvalue().startswith("usage:")


def test_default_config_is_not_created_when_incomplete_args_are_passed_in(monkeypatch):
    monkeypatch.setattr("sys.argv", ["main", "race"])
    with contextlib.redirect_stderr(StringIO()) as temp_stderr:
        with pytest.raises(SystemExit) as cm:
            qbitquick.qbit_quick.main()
        assert cm.value.code == 2
        assert "the following arguments are required: torrent_hash" in temp_stderr.getvalue()


def test_default_config_is_created_if_one_does_not_exist(mock_config_dir, mocker, monkeypatch):
    mocker.patch("qbitquick.qbit_quick.os.path.exists", return_value=False)
    mock_makedirs = mocker.patch("qbitquick.qbit_quick.os.makedirs")
    monkeypatch.setattr("sys.argv", ["main", "config", "--print"])
    with patch("builtins.open", mock_open(read_data="{}")) as mock_file:
        handle = mock_file.return_value
        qbitquick.qbit_quick.main()

        mock_makedirs.assert_called_once_with(mock_config_dir(), exist_ok=True)
        handle.write.assert_called()  # Assert the default file is written

        mock_file.assert_called_with(os.path.join(mock_config_dir(), "config.json"), "r")
        handle.read.assert_called()  # Assert the newly written file is read


def test_connect_with_host_but_no_port(mocker):
    config = {
        "host": "localhost",
        "username": "admin",
        "password": "password",
        "unrelated_key": "should_not_be_included"  # This should be filtered out
    }

    mock_client_class = mocker.patch("qbitquick.qbit_quick.Client")
    qbitquick.qbit_quick.connect(config)

    mock_client_class.assert_called_once_with(host="localhost", username="admin", password="password")


def test_connect_with_separate_host_and_port(mocker):
    config = {
        "host": "localhost",
        "port": "1234",
        "username": "admin",
        "password": "password",
        "unrelated_key": "should_not_be_included"  # This should be filtered out
    }

    mock_client_class = mocker.patch("qbitquick.qbit_quick.Client")
    qbitquick.qbit_quick.connect(config)

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
def test_successful_race(mock_client_instance, override_config, mock_config, category, torrent_factory, tracker_factory,
                         mock_get_db_connection, mocker, monkeypatch):
    # Setup torrents
    racing_torrent = torrent_factory(
        category=category, name="racing_torrent", state=TorrentState.CHECKING_DOWNLOAD
    )
    downloading_torrent = torrent_factory(
        category=category, state=TorrentState.DOWNLOADING, ratio=1.0, progress=0.5, name="downloading_torrent"
    )
    ignored_torrent = torrent_factory(
        category="ignore", state=TorrentState.UPLOADING, ratio=1.0, progress=1.0, name="ignored_torrent"
    )
    non_racing_torrent = torrent_factory(
        category="other", state=TorrentState.DOWNLOADING, progress=0.5, name="non_racing_torrent"
    )
    paused_torrent = torrent_factory(
        category=category, state=TorrentState.PAUSED_DOWNLOAD, ratio=1.0, progress=0.5, name="paused_torrent"
    )
    uploading_torrent = torrent_factory(
        category=category, state=TorrentState.UPLOADING, progress=1.0, ratio=2.0, name="uploading_torrent"
    )
    torrents = [
        racing_torrent, non_racing_torrent, downloading_torrent, uploading_torrent, paused_torrent, ignored_torrent
    ]

    conn, cur = initialise_mock_db(mock_get_db_connection)

    # Setup trackers
    working_tracker = tracker_factory(
        status=TrackerStatus.NOT_CONTACTED, url="working_tracker"
    )
    disabled_tracker = tracker_factory(
        status=TrackerStatus.DISABLED, url="disabled_tracker"
    )
    not_working_tracker = tracker_factory(
        status=TrackerStatus.NOT_WORKING, url="not_working_tracker", msg="reason"
    )
    trackers = [
        working_tracker, disabled_tracker, not_working_tracker
    ]

    # Update the trackers after each loop by hooking into the sleep function
    def sleep_callback(_seconds):
        racing_torrent.state = TorrentState.DOWNLOADING
        update_tracker(working_tracker)

    mocker.patch.object(qbitquick.qbit_quick, "time", wraps=time)
    mock_sleep = mocker.patch("qbitquick.qbit_quick.time.sleep", side_effect=sleep_callback)

    # Various callbacks can update the tracker status, so this list needs to match exactly how many callback there are
    tracker_statuses = iter([
        # Sleep called while torrent is checking, so don't change the status
        {},
        # Reannounce changes the status to updating
        {"status": TrackerStatus.UPDATING},
        # Sleeping after reannounce changes the status to not working
        {"status": TrackerStatus.NOT_WORKING, "msg": "unregistered"},
        # Recheck due to unregistered changes the status to updating and clears the message
        {"status": TrackerStatus.UPDATING, "msg": None},
        # Sleeping while updating changes the status to not working
        {"status": TrackerStatus.NOT_WORKING, "msg": "too many requests"},
        # Sleeping due to too many requests changes the status to updating and clears the message
        {"status": TrackerStatus.UPDATING, "msg": None},
        # Sleeping while updating changes the status to working
        {"status": TrackerStatus.WORKING},
    ])

    def torrents_info_side_effect(**kwargs):
        if not kwargs:
            return TorrentInfoList(torrents)
        if kwargs.get("torrent_hashes") == racing_torrent.hash:
            return TorrentInfoList([racing_torrent])
        return None

    def torrents_trackers_side_effect(**kwargs):
        if kwargs.get("torrent_hash") == racing_torrent.hash:
            return TrackersList(trackers)
        return None

    def update_tracker(tracker):
        merge_and_remove(tracker, next(tracker_statuses))

    def torrents_pause_side_effect(**kwargs):
        hash_to_torrent = {torrent.hash: torrent for torrent in torrents}
        for torrent_hash in kwargs["torrent_hashes"]:
            if torrent := hash_to_torrent.get(torrent_hash):
                torrent.state = TorrentState.PAUSED_DOWNLOAD

    mock_client_instance.torrents_info.side_effect = torrents_info_side_effect
    mock_client_instance.torrents_trackers.side_effect = torrents_trackers_side_effect
    mock_client_instance.torrents_reannounce.side_effect = lambda **kwargs: update_tracker(working_tracker)
    mock_client_instance.torrents_recheck.side_effect = lambda **kwargs: update_tracker(working_tracker)
    mock_client_instance.torrents_pause.side_effect = torrents_pause_side_effect

    # Call the main function with the race command
    mocker.patch("qbitquick.qbit_quick.os.path.exists", return_value=True)
    monkeypatch.setattr("sys.argv", ["main", "race", racing_torrent.hash])
    exit_code = qbitquick.qbit_quick.main()

    # Print out the torrents, so we have all the information if an assertion fails
    print(json.dumps(torrents))

    # Verify the script exited with a successful exit code
    assert exit_code == 0
    # Verify reannounce was called on the torrent, as it was in a NOT_WORKING state
    mock_client_instance.torrents_reannounce.assert_called_once_with(torrent_hashes=racing_torrent.hash)
    # Verify recheck was called on the torrent, as the tracker reported the torrent was "unregistered"
    mock_client_instance.torrents_recheck.assert_called_once_with(torrent_hashes=racing_torrent.hash)
    # Verify pause was only called on the torrents eligible for pausing
    assert_called_once_with_in_any_order(mock_client_instance.torrents_pause, torrent_hashes=[
        downloading_torrent.hash,
        non_racing_torrent.hash,
        uploading_torrent.hash,
    ])
    # Verify the racing torrent hash was added to the database
    cur.execute("SELECT racing_torrent_hash FROM racing_torrents")
    assert cur.fetchall() == [(racing_torrent.hash,)]
    # Verify all the paused torrent hashes were added to the database
    cur.execute("SELECT paused_torrent_hash FROM paused_torrents ORDER BY paused_torrent_hash")
    assert cur.fetchall() == sorted([
        (downloading_torrent.hash,),
        (non_racing_torrent.hash,),
        (uploading_torrent.hash,),
    ])
    # Verify the script waited for TOO_MANY_REQUESTS_DELAY seconds, as the tracker returned "too many requests"
    mock_sleep.assert_any_call(TOO_MANY_REQUESTS_DELAY)


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
def test_racing_paused_torrent(mock_client_instance, override_config, mock_config, torrent_factory, tracker_factory,
                         mock_get_db_connection, mocker, monkeypatch):
    # Setup torrents
    racing_torrent = torrent_factory(
        category="race", name="racing_torrent", state=TorrentState.CHECKING_DOWNLOAD
    )
    downloading_torrent = torrent_factory(
        category="race", state=TorrentState.DOWNLOADING, ratio=1.0, progress=0.5, name="downloading_torrent"
    )
    ignored_torrent = torrent_factory(
        category="ignore", state=TorrentState.UPLOADING, ratio=1.0, progress=1.0, name="ignored_torrent"
    )
    non_racing_torrent = torrent_factory(
        category="other", state=TorrentState.DOWNLOADING, progress=0.5, name="non_racing_torrent"
    )
    paused_torrent = torrent_factory(
        category="race", state=TorrentState.PAUSED_DOWNLOAD, ratio=1.0, progress=0.5, name="paused_torrent"
    )
    uploading_torrent = torrent_factory(
        category="race", state=TorrentState.UPLOADING, progress=1.0, ratio=2.0, name="uploading_torrent"
    )
    torrents = [
        racing_torrent, non_racing_torrent, downloading_torrent, uploading_torrent, paused_torrent, ignored_torrent
    ]

    conn, cur = initialise_mock_db(mock_get_db_connection)

    # Setup trackers
    working_tracker = tracker_factory(
        status=TrackerStatus.NOT_CONTACTED, url="working_tracker"
    )

    # Update the trackers after each loop by hooking into the sleep function
    def sleep_callback(_seconds):
        racing_torrent.state = TorrentState.PAUSED_DOWNLOAD
        working_tracker.update(next(tracker_statuses))

    mocker.patch.object(qbitquick.qbit_quick, "time", wraps=time)
    mocker.patch("qbitquick.qbit_quick.time.sleep", side_effect=sleep_callback)

    tracker_statuses = cycle([
        {"status": TrackerStatus.WORKING},
    ])

    def torrents_info_side_effect(**kwargs):
        if not kwargs:
            return TorrentInfoList(torrents)
        if kwargs.get("torrent_hashes") == racing_torrent.hash:
            return TorrentInfoList([racing_torrent])
        return None

    mock_client_instance.torrents_info.side_effect = torrents_info_side_effect

    # Call the main function with the race command
    mocker.patch("qbitquick.qbit_quick.os.path.exists", return_value=True)
    monkeypatch.setattr("sys.argv", ["main", "race", racing_torrent.hash])
    exit_code = qbitquick.qbit_quick.main()

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
def test_racing_torrent_with_invalid_category(category, mock_client_instance, override_config, mock_config, torrent_factory,
                                         tracker_factory, mock_get_db_connection, mocker, monkeypatch):
    # Setup racing torrent with no category
    racing_torrent = torrent_factory(
        category=category, name="racing_torrent", state=TorrentState.CHECKING_DOWNLOAD
    )
    conn, cur = initialise_mock_db(mock_get_db_connection)

    mocker.patch.object(qbitquick.qbit_quick, "time", wraps=time)

    mock_client_instance.torrents_info.return_value = TorrentInfoList([racing_torrent])

    # Call the main function with the race command
    mocker.patch("qbitquick.qbit_quick.os.path.exists", return_value=True)
    monkeypatch.setattr("sys.argv", ["main", "race", racing_torrent.hash])
    exit_code = qbitquick.qbit_quick.main()

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


def test_post_race(mock_client_instance, torrent_factory, mock_get_db_connection, mocker, monkeypatch):
    racing_torrent = torrent_factory(
        category="race", name="racing_torrent"
    )
    paused_torrent = torrent_factory(
        category="race", state=TorrentState.PAUSED_DOWNLOAD, ratio=1.0, progress=0.5, name="paused_torrent"
    )
    torrents = [racing_torrent, paused_torrent]

    paused_torrent_hashes = [paused_torrent.hash, "missing_torrent_hash"]
    conn, cur = initialise_mock_db(mock_get_db_connection, racing_torrent.hash, paused_torrent_hashes)

    def torrents_info_side_effect(**kwargs):
        return TorrentInfoList([t for t in torrents if t.hash in kwargs["torrent_hashes"]])

    mock_client_instance.torrents_info.side_effect = torrents_info_side_effect

    # Call the main function with the post_race command
    command = "post_race"
    exit_code = qbit_quick_main(command, racing_torrent, mocker, monkeypatch)

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


def qbit_quick_main(command, racing_torrent, mocker, monkeypatch):
    mocker.patch("qbitquick.qbit_quick.os.path.exists", return_value=True)
    monkeypatch.setattr("sys.argv", ["main", command, racing_torrent.hash])
    exit_code = qbitquick.qbit_quick.main()
    return exit_code


def test_post_race_with_no_torrents_to_resume(mock_client_instance, sample_config, torrent_factory, mock_get_db_connection):
    racing_torrent = torrent_factory(category="race", name="racing_torrent")

    conn, cur = initialise_mock_db(mock_get_db_connection, racing_torrent.hash, [])

    mock_client_instance.torrents_info.return_value = TorrentInfoList([racing_torrent])

    exit_code = qbitquick.qbit_quick.post_race(sample_config, racing_torrent.hash)

    # Verify the script exited with a successful exit code
    assert exit_code == 0
    # Verify resume was not called
    mock_client_instance.torrents_resume.assert_not_called()
    # Verify the racing torrent hash was removed from the database
    cur.execute("SELECT * FROM racing_torrents")
    assert cur.fetchall() == []


def test_post_race_with_unknown_hash(mock_client_instance, sample_config, torrent_factory, mock_get_db_connection):
    mock_client_instance.torrents_info.return_value = TorrentInfoList([])

    exit_code = qbitquick.qbit_quick.post_race(sample_config, "unknown_hash")

    # Verify the script exited with an error exit code
    assert exit_code == 1
    # Verify resume was not called
    mock_client_instance.torrents_resume.assert_not_called()


def test_print_db(mock_get_db_connection, torrent_factory, mocker, monkeypatch):
    mocker.patch("qbitquick.qbit_quick.os.path.exists", return_value=True)
    monkeypatch.setattr("sys.argv", ["main", "db", "--print"])

    torrent1 = torrent_factory()
    torrent2 = torrent_factory()

    initialise_mock_db(mock_get_db_connection, torrent1.hash, [torrent2.hash])

    with contextlib.redirect_stdout(StringIO()) as temp_stdout:
        qbitquick.qbit_quick.main()
        assert re.search(fr"{torrent1.hash}.*{torrent2.hash}", temp_stdout.getvalue())


def test_clear_db_clears_the_db_if_input_is_y(mock_get_db_connection, torrent_factory, mocker, monkeypatch):
    mocker.patch("qbitquick.qbit_quick.os.path.exists", return_value=True)
    monkeypatch.setattr("sys.argv", ["main", "db", "--clear"])
    monkeypatch.setattr("builtins.input", lambda _: "y")

    torrent1 = torrent_factory()
    torrent2 = torrent_factory()

    conn, cur = initialise_mock_db(mock_get_db_connection, torrent1.hash, [torrent2.hash])

    qbitquick.qbit_quick.main()

    cur.execute("SELECT * FROM racing_torrents")
    assert cur.fetchall() == []
    cur.execute("SELECT * FROM paused_torrents")
    assert cur.fetchall() == []


def test_clear_db_does_nothing_if_input_is_not_y(mock_get_db_connection, torrent_factory, mocker, monkeypatch):
    mocker.patch("qbitquick.qbit_quick.os.path.exists", return_value=True)
    monkeypatch.setattr("sys.argv", ["main", "db", "--clear"])
    monkeypatch.setattr("builtins.input", lambda _: "n")

    torrent1 = torrent_factory()
    torrent2 = torrent_factory()

    conn, cur = initialise_mock_db(mock_get_db_connection, torrent1.hash, [torrent2.hash])

    qbitquick.qbit_quick.main()

    cur.execute("SELECT racing_torrent_hash FROM racing_torrents")
    assert cur.fetchall() == [(torrent1.hash,)]
    cur.execute("SELECT paused_torrent_hash FROM paused_torrents")
    assert cur.fetchall() == [(torrent2.hash,)]


def initialise_mock_db(mock_get_db_connection, racing_torrent_hash=None, paused_torrent_hashes=None):
    """Create a connection to the in-memory database, create the tables and preload them with the provided data"""
    if paused_torrent_hashes is None:
        paused_torrent_hashes = []
    conn, cur = mock_get_db_connection

    ddl_file = resources.files("qbitquick") / "resources" / "race.ddl"
    with ddl_file.open("r") as f:
        cur.executescript(f.read())
    if racing_torrent_hash and paused_torrent_hashes:
        cur.execute("BEGIN TRANSACTION")
        cur.execute("""
                INSERT INTO racing_torrents (racing_torrent_hash)
                VALUES (?)
            """, (racing_torrent_hash,))
        cur.executemany("""
                INSERT INTO paused_torrents (racing_torrent_hash, paused_torrent_hash)
                VALUES (?, ?)
            """, [(racing_torrent_hash, paused_torrent_hash,) for paused_torrent_hash in paused_torrent_hashes])
        conn.commit()

    return conn, cur