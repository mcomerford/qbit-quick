import json
import os
import re
import threading
import uuid
from datetime import timedelta
from pathlib import Path, PurePath
from sqlite3 import Connection, Cursor
from typing import Any, Callable, Iterator
from unittest.mock import ANY, MagicMock, mock_open, patch

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from pytest_mock import MockerFixture
from qbittorrentapi import TorrentDictionary, TorrentInfoList, TorrentState, Tracker, TrackerStatus, TrackersList
from starlette.status import HTTP_200_OK, HTTP_400_BAD_REQUEST
from starlette.templating import Jinja2Templates

from conftest import initialise_mock_db
from qbitquick.config import TOO_MANY_REQUESTS_DELAY
from qbitquick.database.database_handler import save_torrent_hashes_to_pause
from qbitquick.routes import task_manager
from test_helpers import assert_called_once_with_in_any_order, calculate_last_activity_time, merge_and_remove, mock_torrents_info, print_torrents


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

    mock_torrents_info(mock_client_instance, torrents)
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

    print_torrents(torrents)

    mock_client_instance.torrents_reannounce.assert_called_once_with(torrent_hashes=racing_torrent.hash)
    # Verify recheck was called on the torrent, as the tracker reported the torrent was "unregistered"
    mock_client_instance.torrents_recheck.assert_called_once_with(torrent_hashes=racing_torrent.hash)
    # Verify pause was only called on the torrents eligible for pausing
    assert_called_once_with_in_any_order(mock_client_instance.torrents_pause, torrent_hashes=[non_racing_torrent.hash, uploading_torrent.hash, ])
    # Verify the racing torrent hash was added to the database
    cur.execute("SELECT id FROM pause_events")
    assert cur.fetchall() == [(racing_torrent.hash,)]
    # Verify all the paused torrent hashes were added to the database
    cur.execute("SELECT torrent_hash FROM paused_torrents ORDER BY torrent_hash")
    assert cur.fetchall() == sorted([(non_racing_torrent.hash,), (uploading_torrent.hash,), ])
    # Verify the script waited for TOO_MANY_REQUESTS_DELAY seconds, as the tracker returned "too many requests"
    mock_sleep.assert_any_call(TOO_MANY_REQUESTS_DELAY, ANY)


def test_post_race(mocker: MockerFixture, test_server: TestClient, mock_client_instance: MagicMock, mock_get_db_connection: tuple[Connection, Cursor],
                   torrent_factory: Callable[..., TorrentDictionary]):
    racing_torrent = torrent_factory(category="race", name="racing_torrent")
    paused_torrent = torrent_factory(category="race", name="paused_torrent", state=TorrentState.PAUSED_DOWNLOAD, ratio=1.0, progress=0.5)
    torrents = [racing_torrent, paused_torrent]

    paused_torrent_hashes = {paused_torrent.hash, "missing_torrent_hash"}
    conn, cur = initialise_mock_db(mock_get_db_connection, racing_torrent.hash, paused_torrent_hashes)

    mock_torrents_info(mock_client_instance, torrents)

    task_uuid = str(uuid.uuid4())
    mocker.patch("qbitquick.task_manager.uuid.uuid4", return_value=uuid.UUID(task_uuid))

    response = test_server.post(f"/post-race/{racing_torrent.hash}")
    assert response.status_code == HTTP_200_OK
    assert response.json() == {
        "status": "success",
        "message": "post race ran successfully"
    }

    # Verify resume was called on the paused torrents, as it was in a NOT_WORKING state
    mock_client_instance.torrents_resume.assert_called_once_with(torrent_hashes={paused_torrent.hash})
    # Verify the racing torrent hash was removed from the database
    cur.execute("SELECT * FROM pause_events")
    assert cur.fetchall() == []
    # Verify the associated paused torrent hashes were removed from the database
    cur.execute("SELECT * FROM paused_torrents")
    assert cur.fetchall() == []


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
def test_pause(test_server: TestClient, mock_client_instance: MagicMock, mock_get_db_connection: tuple[Connection, Cursor], torrent_factory: Callable[..., TorrentDictionary],
               override_config: dict[str, Any], with_id: bool):
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
        response = test_server.post(f"/pause/{event_id}")
    else:
        event_id = "pause"
        response = test_server.post(f"/pause")

    assert response.status_code == HTTP_200_OK
    assert response.json() == {
        "status": "success",
        "message": "torrents paused successfully"
    }

    print_torrents(torrents)

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
def test_unpause(test_server: TestClient, mock_client_instance: MagicMock, mock_get_db_connection: tuple[Connection, Cursor], torrent_factory: Callable[..., TorrentDictionary],
                 with_id: bool):
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
        response = test_server.post(f"/unpause/{event_id}")
    else:
        event_id = "pause"
        save_torrent_hashes_to_pause(event_id, paused_torrents)
        response = test_server.post(f"/unpause")

    assert response.status_code == HTTP_200_OK
    assert response.json() == {
        "status": "success",
        "message": "torrents unpaused successfully"
    }

    print_torrents(torrents)

    # Verify resume was only called on other_paused_torrent, as paused_torrent is still paused due to racing_torrent
    mock_client_instance.torrents_resume.assert_called_once_with(torrent_hashes={other_paused_torrent.hash})
    # Verify the pause event was removed from the database
    cur.execute(f"SELECT * FROM pause_events WHERE id = '{event_id}'")
    assert cur.fetchall() == []
    # Verify the associated paused torrent hashes were removed from the database
    cur.execute(f"SELECT * FROM paused_torrents WHERE id = '{event_id}'")
    assert cur.fetchall() == []


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
def test_info(test_server: TestClient, mock_client_instance: MagicMock, mock_get_db_connection: tuple[Connection, Cursor], torrent_factory: Callable[..., TorrentDictionary],
              status: str, include_field_names: bool, fields: list[str] | None, format: str):
    host_path = str(PurePath("/mock/host_path"))
    container_path = str(PurePath("/mock/container_path"))
    container_path2 = str(PurePath("/mock/container_path2"))

    # Setup torrents
    racing_torrent = torrent_factory(name="racing_torrent", content_path=container_path, state=TorrentState.CHECKING_DOWNLOAD)
    downloading_torrent = torrent_factory(name="downloading_torrent", content_path=container_path2, state=TorrentState.DOWNLOADING, ratio=1.0, progress=0.5)
    ignored_torrent = torrent_factory(name="ignored_torrent", content_path=container_path, state=TorrentState.UPLOADING, ratio=1.0, progress=1.0)
    non_racing_torrent = torrent_factory(name="non_racing_torrent", content_path=container_path, state=TorrentState.DOWNLOADING, progress=0.5)
    app_paused_torrent = torrent_factory(name="app_paused_torrent", content_path=container_path, state=TorrentState.PAUSED_UPLOAD, ratio=1.0)
    manually_paused_torrent = torrent_factory(name="manually_paused_torrent", content_path=container_path, state=TorrentState.PAUSED_UPLOAD, ratio=1.0)
    uploading_torrent = torrent_factory(name="uploading_torrent", content_path=container_path, state=TorrentState.UPLOADING, ratio=2.0, progress=1.0)
    torrents = [racing_torrent, non_racing_torrent, downloading_torrent, uploading_torrent, app_paused_torrent, manually_paused_torrent, ignored_torrent]

    def torrents_info_side_effect(*args: list[str], **_kwargs: dict[str, Any]) -> TorrentInfoList | None:
        if args:
            match args[0]:
                case "paused":
                    return TorrentInfoList([t for t in torrents if t.state_enum.is_paused])
        return TorrentInfoList(torrents)

    mock_client_instance.torrents_info.side_effect = torrents_info_side_effect

    params: dict[str, Any] = {
        "status": status,
        "format": format
    }
    if include_field_names:
        params["include_field_names"] = True
    if fields:
        params["fields"] = fields
    response = test_server.get("/info", params=params)

    print_torrents(torrents)

    assert response.status_code == HTTP_200_OK

    if format == "json":
        if include_field_names:
            expected_json = re.sub(r"\bcontainer_path\b", "host_path", json.dumps(torrents))
            assert response.json() == json.loads(expected_json)
        else:
            expected = [
                [app_paused_torrent["name"], app_paused_torrent["state"], app_paused_torrent["ratio"]],
                [manually_paused_torrent["name"], manually_paused_torrent["state"], manually_paused_torrent["ratio"]]
            ]
            assert response.json() == expected
    elif format == "plain":
        if include_field_names:
            assert response.text == (
                    "category,content_path,hash,name,progress,ratio,state" + os.linesep +
                    f"race,{host_path},{racing_torrent.hash},racing_torrent,0,0,TorrentState.CHECKING_DOWNLOAD" + os.linesep +
                    f"race,{host_path},{non_racing_torrent.hash},non_racing_torrent,0.5,0,TorrentState.DOWNLOADING" + os.linesep +
                    f"race,{container_path2},{downloading_torrent.hash},downloading_torrent,0.5,1.0,TorrentState.DOWNLOADING" + os.linesep +
                    f"race,{host_path},{uploading_torrent.hash},uploading_torrent,1.0,2.0,TorrentState.UPLOADING" + os.linesep +
                    f"race,{host_path},{app_paused_torrent.hash},app_paused_torrent,0,1.0,TorrentState.PAUSED_UPLOAD" + os.linesep +
                    f"race,{host_path},{manually_paused_torrent.hash},manually_paused_torrent,0,1.0,TorrentState.PAUSED_UPLOAD" + os.linesep +
                    f"race,{host_path},{ignored_torrent.hash},ignored_torrent,1.0,1.0,TorrentState.UPLOADING"
            )
        else:
            assert response.text == (
                    "app_paused_torrent,TorrentState.PAUSED_UPLOAD,1.0" + os.linesep +
                    "manually_paused_torrent,TorrentState.PAUSED_UPLOAD,1.0"
            )


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

    conn, cur = initialise_mock_db(mock_get_db_connection, racing_torrent.hash, {paused_torrent1.hash, paused_torrent2.hash})

    response = test_server.delete("/db")

    assert response.status_code == HTTP_200_OK
    assert response.json() == {
            "status": "success",
            "message": "database cleared"
        }

    # Verify all pause events were removed from the database
    cur.execute("SELECT * FROM pause_events")
    assert cur.fetchall() == []
    # Verify all paused torrent hashes were removed from the database
    cur.execute("SELECT * FROM paused_torrents")
    assert cur.fetchall() == []


def test_delete_one_entry_from_db(mock_get_db_connection: tuple[Connection, Cursor], test_server: TestClient, torrent_factory: Callable[..., TorrentDictionary]):
    racing_torrent1 = torrent_factory()
    racing_torrent2 = torrent_factory()
    paused_torrent1 = torrent_factory()
    paused_torrent2 = torrent_factory()
    paused_torrent3 = torrent_factory()

    conn, cur = initialise_mock_db(mock_get_db_connection, racing_torrent1.hash, {paused_torrent1.hash, paused_torrent2.hash})
    save_torrent_hashes_to_pause(racing_torrent2.hash, {paused_torrent2.hash, paused_torrent3.hash})

    response = test_server.delete(f"/db/{racing_torrent1.hash}")

    assert response.status_code == HTTP_200_OK
    assert response.json() == {
            "status": "success",
            "message": f"{racing_torrent1.hash} deleted from database"
        }

    # Verify only racing_torrent2 remains in the database
    cur.execute("SELECT id FROM pause_events")
    assert cur.fetchall() == [(racing_torrent2.hash,)]
    # Verify only paused_torrent2 and paused_torrent3 remain in the database
    cur.execute("SELECT torrent_hash FROM paused_torrents ORDER BY torrent_hash")
    assert cur.fetchall() == sorted([(paused_torrent2.hash,), (paused_torrent3.hash,), ])


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
                                   'method': ['POST'],
                                   'path': '/pause/{event_id}'
                               }, {
                                   'method': ['POST'],
                                   'path': '/pause'
                               }, {
                                   'method': ['POST'],
                                   'path': '/unpause/{event_id}'
                               }, {
                                   'method': ['POST'],
                                   'path': '/unpause'
                               }, {
                                   'method': ['GET'],
                                   'path': '/info'
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


def test_get_config(test_server: TestClient, override_config: dict[str, Any]):
    response = test_server.get("/config")
    assert response.status_code == HTTP_200_OK
    assert response.json() == override_config


def test_get_db(monkeypatch: MonkeyPatch, mock_get_db_connection: tuple[Connection, Cursor], test_server: TestClient, torrent_factory: Callable[..., TorrentDictionary]):
    templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")
    monkeypatch.setattr("qbitquick.routes.templates", templates)

    racing_torrent = torrent_factory()
    paused_torrent1 = torrent_factory()
    paused_torrent2 = torrent_factory()

    initialise_mock_db(mock_get_db_connection, racing_torrent.hash, {paused_torrent1.hash, paused_torrent2.hash})

    response = test_server.get("/db")

    assert response.status_code == HTTP_200_OK

    context = response.context  # type: ignore

    assert context["headers"] == ["pause_event_id", "paused_torrent_hashes"]
    sorted_paused_torrent_hashes = sorted([paused_torrent1.hash, paused_torrent2.hash])
    assert context["rows"] == [
        [racing_torrent.hash, os.linesep.join(sorted_paused_torrent_hashes)]
    ]


def test_save_config(mocker: MockerFixture, sample_config: dict[str, Any], mock_config_path: Path, mock_get_db_connection: tuple[Connection, Cursor], test_server: TestClient,
                     torrent_factory: Callable[..., TorrentDictionary]):
    new_config = sample_config.copy()
    new_config["ignore_categories"] = ["ignore1", "ignore2"]

    mocker.patch("pathlib.Path.exists", return_value=True)

    mock_file = mock_open(read_data=json.dumps(sample_config))
    with patch("builtins.open", mock_file), patch("qbitquick.config.Path.open", mock_file):
        response = test_server.post("/config", json=new_config)

        assert response.status_code == HTTP_200_OK
        assert response.json() == {
            "status": "success",
            "message": f"config successfully saved to: {mock_config_path / "config.json"}",
        }

        mock_file.assert_any_call("w")
        handle = mock_file()
        written = "".join(call.args[0] for call in handle.write.call_args_list)
        assert written == json.dumps(new_config, indent=2)