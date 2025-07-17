import json
import threading
import uuid
from sqlite3 import Connection, Cursor
from typing import Any, Callable, Iterator
from unittest.mock import ANY, MagicMock

from fastapi.testclient import TestClient
from pytest_mock import MockerFixture
from qbittorrentapi import TorrentDictionary, TorrentInfoList, TorrentState, Tracker, TrackerStatus, TrackersList
from starlette.status import HTTP_200_OK, HTTP_400_BAD_REQUEST

from conftest import initialise_mock_db
from qbitquick.config import TOO_MANY_REQUESTS_DELAY
from qbitquick.routes import task_manager
from test_helpers import assert_called_once_with_in_any_order, merge_and_remove


def test_race(mocker: MockerFixture, test_server: TestClient, mock_client_instance: MagicMock, mock_get_db_connection: tuple[Connection, Cursor],
              torrent_factory: Callable[..., TorrentDictionary], tracker_factory: Callable[..., Tracker]):
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
    disabled_tracker = tracker_factory(status=TrackerStatus.DISABLED, url="disabled_tracker")
    not_working_tracker = tracker_factory(status=TrackerStatus.NOT_WORKING, url="not_working_tracker", msg="reason")
    trackers = [working_tracker, disabled_tracker, not_working_tracker]

    # Update the trackers after each loop by hooking into the sleep function
    def sleep_callback(_timeout: float | None = None, _stop_event: threading.Event | None = None, *_args: list[str] | None, **_kwargs: dict[str, Any] | None) -> bool:
        racing_torrent.state = TorrentState.DOWNLOADING
        update_tracker(working_tracker)
        return False

    mock_sleep = mocker.patch("qbitquick.handlers.interruptible_sleep", side_effect=sleep_callback)

    task_uuid = str(uuid.uuid4())
    mocker.patch("qbitquick.task_manager.uuid.uuid4", return_value=uuid.UUID(task_uuid))

    # Various callbacks can update the tracker status, so this list needs to match exactly how many callback there are
    tracker_statuses: Iterator[dict[str, Any]] = iter([
        # Wait called while torrent is checking, so don't change the status
        {},
        # Reannounce changes the status to updating
        {
            "status": TrackerStatus.UPDATING
        },
        # Wait after reannounce changes the status to not working
        {
            "status": TrackerStatus.NOT_WORKING,
            "msg": "unregistered"
        },
        # Recheck due to unregistered changes the status to updating and clears the message
        {
            "status": TrackerStatus.UPDATING,
            "msg": None
        },
        # Waiting while updating changes the status to not working
        {
            "status": TrackerStatus.NOT_WORKING,
            "msg": "too many requests"
        },
        # Waiting due to too many requests changes the status to updating and clears the message
        {
            "status": TrackerStatus.UPDATING,
            "msg": None
        },
        # Waiting while updating changes the status to working
        {
            "status": TrackerStatus.WORKING
        },
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

    response = test_server.post(f"/race/{racing_torrent.hash}")
    assert response.status_code == HTTP_200_OK
    assert response.json() == {
        "status": "accepted",
        "task_id": f"{task_uuid}"
    }

    task_manager.join_all_threads(10.0)

    # Print out the torrents, so we have all the information if an assertion fails
    print(json.dumps(torrents))

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


def test_post_race(mocker: MockerFixture, test_server: TestClient, mock_client_instance: MagicMock, mock_get_db_connection: tuple[Connection, Cursor],
                   torrent_factory: Callable[..., TorrentDictionary], run_main: Callable[..., int]):
    racing_torrent = torrent_factory(category="race", name="racing_torrent")
    paused_torrent = torrent_factory(category="race", name="paused_torrent", state=TorrentState.PAUSED_DOWNLOAD, ratio=1.0, progress=0.5)
    torrents = [racing_torrent, paused_torrent]

    paused_torrent_hashes = [paused_torrent.hash, "missing_torrent_hash"]
    conn, cur = initialise_mock_db(mock_get_db_connection, racing_torrent.hash, paused_torrent_hashes)

    def torrents_info_side_effect(**kwargs: dict[str, Any]) -> TorrentInfoList | None:
        return TorrentInfoList([t for t in torrents if t.hash in kwargs["torrent_hashes"]])

    mock_client_instance.torrents_info.side_effect = torrents_info_side_effect

    task_uuid = str(uuid.uuid4())
    mocker.patch("qbitquick.task_manager.uuid.uuid4", return_value=uuid.UUID(task_uuid))

    response = test_server.post(f"/post-race/{racing_torrent.hash}")
    assert response.status_code == HTTP_200_OK
    assert response.json() == {
        "status": "success",
        "message": "post race ran successfully",
    }

    task_manager.join_all_threads(10.0)

    # Verify resume was called on the paused torrents, as it was in a NOT_WORKING state
    mock_client_instance.torrents_resume.assert_called_once_with(torrent_hashes={paused_torrent.hash})
    # Verify the racing torrent hash was removed from the database
    cur.execute("SELECT * FROM racing_torrents")
    assert cur.fetchall() == []
    # Verify the associated paused torrent hashes was removed from the database
    cur.execute("SELECT * FROM paused_torrents")
    assert cur.fetchall() == []


def test_get_running_tasks(test_server: TestClient):
    # Start an interruptable wait
    task_id1 = task_manager.start_task(lambda stop_event: stop_event.wait(), task_name="task1")
    task_id2 = task_manager.start_task(lambda stop_event: stop_event.wait(), task_name="task2")
    # Call the cancel route to set the stop_event and end the task
    running_tasks = test_server.get("/tasks")
    assert running_tasks.status_code == HTTP_200_OK
    assert running_tasks.json() == {
        "task1": task_id1,
        "task2": task_id2,
    }


def test_cancel_task(test_server: TestClient):
    # Start an interruptable wait
    task_id = task_manager.start_task(lambda stop_event: stop_event.wait(), task_name="task1")
    # Call the cancel route to set the stop_event and end the task
    cancel_response = test_server.post(f"/cancel/{task_id}")
    assert cancel_response.status_code == HTTP_200_OK
    assert cancel_response.json() == {
        "status": "success",
        "message": "task successfully cancelled"
    }


def test_cancel_task_that_does_not_exist(test_server: TestClient):
    task_id = uuid.uuid4()
    response = test_server.post(f"/cancel/{task_id}")
    assert response.status_code == HTTP_400_BAD_REQUEST
    assert response.json() == {
        "detail": f"No task found with id {task_id}"
    }


def test_delete_all_entries_from_db(mock_get_db_connection: tuple[Connection, Cursor], test_server: TestClient, torrent_factory: Callable[..., TorrentDictionary]):
    racing_torrent = torrent_factory()
    paused_torrent1 = torrent_factory()
    paused_torrent2 = torrent_factory()

    conn, cur = initialise_mock_db(mock_get_db_connection, racing_torrent.hash, [paused_torrent1.hash, paused_torrent2.hash])

    response = test_server.delete("/db")

    assert response.status_code == HTTP_200_OK
    assert response.json() == {
            "status": "success",
            "message": "database cleared"
        }

    cur.execute("SELECT * FROM racing_torrents")
    assert cur.fetchall() == []
    cur.execute("SELECT * FROM paused_torrents")
    assert cur.fetchall() == []


def test_delete_one_entry_from_db(mock_get_db_connection: tuple[Connection, Cursor], test_server: TestClient, torrent_factory: Callable[..., TorrentDictionary]):
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
                    """, [(racing_torrent2.hash, paused_torrent1.hash),(racing_torrent2.hash, paused_torrent2.hash)])
    conn.commit()

    response = test_server.delete(f"/db/{racing_torrent1.hash}")

    assert response.status_code == HTTP_200_OK
    assert response.json() == {
            "status": "success",
            "message": f"{racing_torrent1.hash} deleted from database"
        }

    cur.execute("SELECT racing_torrent_hash FROM racing_torrents")
    assert cur.fetchall() == [(racing_torrent2.hash,)]
    cur.execute("SELECT paused_torrent_hash FROM paused_torrents ORDER BY paused_torrent_hash")
    assert cur.fetchall() == sorted([(paused_torrent1.hash,), (paused_torrent2.hash,), ])


def test_list_routes(test_server: TestClient):
    response = test_server.get("/")
    assert response.status_code == HTTP_200_OK
    assert response.json() == [{
                                   'method': ['POST'],
                                   'path': '/race/{torrent_hash}'
                               }, {
                                   'method': ['POST'],
                                   'path': '/post-race/{torrent_hash}'
                               }, {
                                   'method': ['DELETE', 'POST'],
                                   'path': '/cancel/{task_id}'
                               }, {
                                   'method': ['GET'],
                                   'path': '/tasks'
                               }, {
                                   'method': ['GET'],
                                   'path': '/config'
                               }, {
                                   'method': ['POST', 'PUT'],
                                   'path': '/config'
                               }, {
                                   'method': ['GET'],
                                   'path': '/db'
                               }, {
                                   'method': ['DELETE'],
                                   'path': '/db/{torrent_hash}'
                               }, {
                                   'method': ['DELETE'],
                                   'path': '/db'
                               }, {
                                   'method': ['GET'],
                                   'path': '/'
                               }]


def test_get_config(test_server: TestClient, mock_config: dict[str, Any]):
    response = test_server.get("/config")
    assert response.status_code == HTTP_200_OK
    assert response.json() == mock_config
