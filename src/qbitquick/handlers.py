import logging.config
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import timedelta
from typing import Any

import uvicorn
from fastapi import FastAPI
from qbittorrentapi import Client, TorrentDictionary, TrackerStatus
from qbittorrentapi.torrents import TorrentStatusesT

from qbitquick.config import TOO_MANY_REQUESTS_DELAY, UNREGISTERED_MESSAGES
from qbitquick.database.database_handler import delete_pause_event, load_all_paused_torrent_hashes, load_torrents_to_unpause, save_torrent_hashes_to_pause
from qbitquick.log_config.logging_config import LOGGING_CONFIG
from qbitquick.task_manager import TaskInterrupted
from qbitquick.utils import interruptible_sleep, is_port_in_use, parse_timedelta

logger = logging.getLogger(__name__)


def connect(config: dict[str, Any]) -> Client:
    try:
        # Filter config to just the qBittorrent connection info
        client_params: set[str] = {"host", "port", "username", "password"}
        conn_info: dict[Any, Any] = {k: v for k, v in config["qbittorrent"].items() if k in client_params}
        client: Client = Client(**conn_info)
        client.auth_log_in()  # This just checks that the connection and login was successful
        logger.info("Connected to qBittorrent successfully")

        logger.info("qBittorrent: %s", client.app.version)
        logger.info("qBittorrent Web API: %s", client.app.webapiVersion)
        if logger.isEnabledFor(logging.DEBUG):
            for k, v in client.app.build_info.items():
                logger.info("%s: %s", k, v)

        return client
    except Exception as e:
        raise ConnectionError("Failed to connect to qBittorrent") from e


def disconnect(client):
    client.auth_log_out()
    logger.info("Logged out of qBittorrent successfully")


def start_server(app: FastAPI, port: int) -> None:
    logger.info("Starting server on port %d", port)
    if is_port_in_use(port):
        raise OSError(f"Port [{port}] already in use")

    uvicorn.run(app, host="0.0.0.0", port=port, log_config=LOGGING_CONFIG)


def race(config: dict[str, Any], racing_torrent_hash: str, stop_event: threading.Event) -> int:
    client = connect(config)

    racing_config = config["racing"]
    race_categories = racing_config["race_categories"] if "race_categories" in racing_config else []
    if not race_categories:
        logger.info("No race categories are set, so all torrents are eligible for racing")

    max_reannounce = racing_config["max_reannounce"] if "max_reannounce" in racing_config else None
    if max_reannounce and max_reannounce > 0:
        logger.info("Maximum number of reannounce requests is set to [%d]", max_reannounce)
    else:
        max_reannounce = None
        logger.info("Maximum number of reannounce requests is set to [Unlimited]")

    reannounce_frequency = racing_config["reannounce_frequency"] if "reannounce_frequency" in racing_config else 5.0
    logger.info("Reannounce frequency set to [%.2f] seconds", reannounce_frequency)

    torrents = client.torrents_info()
    racing_torrent = next((t for t in torrents if t.hash == racing_torrent_hash), None)
    if not racing_torrent:
        logger.error("No torrent found with hash [%s]", racing_torrent_hash)
        return 1
    torrents.remove(racing_torrent)

    # Check the category on the race torrent
    if race_categories:
        if not racing_torrent.category:
            logger.info("Not racing torrent [%s], as no category is set. Valid race categories are: %s", racing_torrent.name, race_categories)
            return 1
        if racing_torrent.category not in race_categories:
            logger.info("Not racing torrent [%s], as category [%s] is not in the list of racing categories %s", racing_torrent.name, racing_torrent.category, race_categories)
            return 1

    pausing = True if "pausing" in racing_config else False
    logger.info("Pausing of torrents before racing is [%s]", "Enabled" if pausing else "Disabled")

    torrent_hashes_to_pause = set()
    if pausing:
        logger.info("Pausing is enabled, so checking which torrents to pause")

        ratio = racing_config["pausing"].get("ratio", 0)
        logger.info("Minimum ratio to be eligible for pausing is set to [%g]", ratio)

        if race_categories:
            logger.info("Valid race categories are %s", race_categories)
        else:
            logger.info("No race categories are set, so all torrents are eligible for pausing")

        ignore_categories = config["ignore_categories"] if "ignore_categories" in config else []
        if ignore_categories:
            logger.info("Ignore categories %s", ignore_categories)

        for torrent in torrents:
            if torrent.category in ignore_categories:
                logger.info("Ignoring torrent [%s], as category [%s] is ignored", torrent.name, torrent.category)
                continue
            if _is_torrent_manually_paused(torrent):
                logger.info("Ignoring torrent [%s], as it is already paused", torrent.name)
                continue
            if race_categories and torrent.category not in race_categories:
                logger.info("Adding torrent [%s] to pause list, as category [%s] is not a valid race category", torrent.name, torrent.category)
                torrent_hashes_to_pause.add(torrent.hash)
            elif torrent.ratio >= ratio:
                if torrent.state_enum.is_downloading:
                    logger.info("Not pausing torrent [%s], as it is still downloading", torrent.name)
                    continue
                logger.info("Adding torrent [%s] to pause list as ratio [%g] >= [%g]", torrent.name, torrent.ratio, ratio)
                torrent_hashes_to_pause.add(torrent.hash)
    else:
        logger.info("Pausing is disabled, so no torrents will be paused")

    # When a new torrent is added, the data will be checked first. Need to wait until this is done.
    # Can be improved if this is ever implemented: https://github.com/qbittorrent/qBittorrent/issues/9177
    while True:
        racing_torrent = _get_torrent(client, racing_torrent_hash)
        if not racing_torrent:
            logger.error("No torrent found with hash [%s]", racing_torrent_hash)
            return 1

        _check_for_interrupt(stop_event, racing_torrent.name)

        if not racing_torrent.state_enum.is_checking:
            break
        logger.debug("Waiting while torrent [%s] is checking...", racing_torrent.name)
        interruptible_sleep(0.1, stop_event)

    if racing_torrent.state_enum.is_paused:
        logger.info("Not racing torrent [%s] as it is paused/stopped", racing_torrent.name)
        return 1
    elif racing_torrent.state_enum.is_complete:
        logger.info("Not racing torrent [%s] as it is already complete", racing_torrent.name)
        return 1

    try:
        save_torrent_hashes_to_pause(racing_torrent_hash, torrent_hashes_to_pause)
    except sqlite3.DatabaseError as e:
        raise IOError(f"Failed to save paused torrent [{racing_torrent.name}] to database") from e

    if torrent_hashes_to_pause:
        logger.info("Pausing [%d] torrents before racing", len(torrent_hashes_to_pause))
        client.torrents_pause(torrent_hashes=torrent_hashes_to_pause)

    # Continually reannounce until the torrent is available in the tracker
    try:
        if not _reannounce_until_working(client, max_reannounce, reannounce_frequency, racing_torrent_hash, stop_event):
            _resume_torrents(client, torrent_hashes_to_pause)
            return 1
    except TaskInterrupted:
        _resume_torrents(client, torrent_hashes_to_pause)
        raise

    logger.info("Racing complete for torrent [%s]", racing_torrent.name)
    disconnect(client)
    return 0


def post_race(config: dict[str, Any], torrent_hash: str) -> int:
    client = connect(config)

    torrent = _get_torrent(client, torrent_hash)
    if not torrent:
        logger.error("No torrent found with hash [%s], so no post race actions can be run", torrent_hash)
        return 1

    torrents_to_unpause = load_torrents_to_unpause(torrent_hash)
    _resume_torrents(client, torrents_to_unpause)
    if delete_pause_event(torrent_hash) > 0:
        logger.info("Deleted pause event: %s", torrent.name)

    logger.info("Post race complete for torrent [%s]", torrent.name)
    disconnect(client)
    return 0


def pause(config: dict[str, Any], event_id: str) -> int:
    client = connect(config)

    torrent_hashes_to_pause = set()

    time_since_active_threshold = timedelta()
    time_active_threshold = timedelta()
    if "pausing" in config:
        pausing_config = config["pausing"]
        if "time_since_active" in pausing_config:
            time_since_active_threshold = parse_timedelta(pausing_config["time_since_active"])
        if "time_active" in pausing_config:
            time_active_threshold = parse_timedelta(pausing_config["time_active"])

    ignore_categories = config["ignore_categories"] if "ignore_categories" in config else []
    if ignore_categories:
        logger.info("Ignore categories %s", ignore_categories)

    torrents = client.torrents_info()
    for torrent in torrents:
        if _is_torrent_manually_paused(torrent):
            logger.info("Ignoring torrent [%s], as it is already paused", torrent.name)
            continue
        if torrent.state_enum.is_downloading:
            logger.info("Ignoring torrent [%s], as it's still downloading", torrent.name)
            continue
        if torrent.category in ignore_categories:
            logger.info("Ignoring torrent [%s], as category [%s] is ignored", torrent.name, torrent.category)
            continue
        time_since_active = timedelta(seconds=(time.time() - torrent.last_activity))
        if time_since_active >= time_since_active_threshold:
            logger.info("Adding torrent [%s] to pause list as time since active [%s] >= [%s]", torrent.name, time_since_active, time_since_active_threshold)
            torrent_hashes_to_pause.add(torrent.hash)
            continue
        time_active = timedelta(seconds=torrent.time_active)
        if time_active >= time_active_threshold:
            logger.info("Adding torrent [%s] to pause list as time active [%s] >= [%s]", torrent.name, time_active, time_active_threshold)
            torrent_hashes_to_pause.add(torrent.hash)
            continue

    save_torrent_hashes_to_pause(event_id, torrent_hashes_to_pause)
    client.torrents_pause(torrent_hashes=torrent_hashes_to_pause)

    disconnect(client)
    return 0


def unpause(config: dict[str, Any], event_id: str) -> int:
    client = connect(config)

    torrents_to_unpause = load_torrents_to_unpause(event_id)
    _resume_torrents(client, torrents_to_unpause)
    if delete_pause_event(event_id) > 0:
        logger.info("Deleted pause event: %s", event_id)

    disconnect(client)
    return 0


def get_torrents_info(config: dict[str, Any], status: TorrentStatusesT = "all", fields: list[str] | None = None,) -> list[dict[str, Any]]:
    client = connect(config)
    torrent_dicts = [dict(t) for t in (client.torrents_info(status))]
    disconnect(client)

    if fields:
        return [{key: t.get(key, "") for key in fields} for t in torrent_dicts]
    else:
        return torrent_dicts


def edit_config(config_path: str) -> int:
    editor = os.environ.get("EDITOR", "vi")
    if sys.platform.startswith("win") and not os.environ.get("EDITOR"):
        editor = "notepad"
    # noinspection PyBroadException
    try:
        subprocess.run([editor, config_path])
    except Exception as e:
        raise IOError(f"Could not open file: {config_path}") from e
    return 0


def _reannounce_until_working(client: Client, max_reannounce: int | None, reannounce_frequency: float, torrent_hash: str, stop_event: threading.Event) -> bool:
    reannounce_count = 0
    while not max_reannounce or reannounce_count < max_reannounce:
        torrent = _get_torrent(client, torrent_hash)
        if not torrent:
            logger.error("Aborting race, as torrent with hash [%s] no longer exists", torrent_hash)
            return False
        if torrent.state_enum.is_stopped:
            logger.error("Aborting race, as torrent [%s] has been stopped", torrent.name)
            return False

        _check_for_interrupt(stop_event, torrent.name)

        has_updating = False
        trackers = client.torrents_trackers(torrent_hash=torrent_hash)
        for status in (tracker.status for tracker in trackers):
            if status == TrackerStatus.WORKING:
                logger.info("Torrent [%s] has at least 1 working tracker", torrent.name)
                return True
            elif status == TrackerStatus.UPDATING:
                has_updating = True

        if has_updating:
            logger.debug("Waiting on torrent [%s] while trackers are updating...", torrent.name)
            interruptible_sleep(reannounce_frequency, stop_event)
            continue
        if _handle_unregistered_torrent(client, torrent) or _handle_too_many_requests(client, torrent, stop_event):
            continue
        if _reannounce(client, torrent):
            logger.info("Torrent [%s] has at least 1 working tracker", torrent.name)
            return True
        reannounce_count += 1
        logger.info("Sent reannounce [%s] of [%s] for torrent [%s]", reannounce_count, max_reannounce if max_reannounce else "Unlimited", torrent.name)
        interruptible_sleep(reannounce_frequency, stop_event)

    torrent = _get_torrent(client, torrent_hash)
    if torrent:
        logger.info("Giving up, as there are still no working trackers for torrent [%s]", torrent.name)
    else:
        logger.info("Giving up, as there are still no working trackers for torrent with hash [%s]", torrent_hash)
    return False


def _handle_unregistered_torrent(client: Client, torrent: TorrentDictionary) -> bool:
    """
    When a new torrent is added, the tracker may state that the torrent is unregistered. In this case,
    reannouncing won't help and the torrent has to be stopped and restarted. Forcing a recheck is an
    easy way to do this.
    """
    not_working_trackers = [tracker for tracker in client.torrents_trackers(torrent_hash=torrent.hash) if tracker.status == TrackerStatus.NOT_WORKING]
    for not_working_tracker in not_working_trackers:
        tracker_msg = not_working_tracker.msg.lower()
        if any(msg in tracker_msg for msg in UNREGISTERED_MESSAGES):
            if torrent.progress == 0:
                logger.info("Torrent [%s] has been marked as [%s] in tracker [%s], so forcing a recheck", torrent.name, not_working_tracker.msg, not_working_tracker.url)
                client.torrents_recheck(torrent_hashes=torrent.hash)
            else:
                logger.info("Torrent [%s] has been marked as [%s] in tracker [%s], so forcing a restart", torrent.name, not_working_tracker.msg, not_working_tracker.url)
                client.torrents_stop(torrent_hashes=torrent.hash)
                client.torrents_start(torrent_hashes=torrent.hash)
            return True
    return False


def _handle_too_many_requests(client: Client, torrent: TorrentDictionary, stop_event: threading.Event) -> bool:
    """
    If too many requests are sent in a short space of time, the tracker will block any further requests.
    It's not clear what the limit it is, but this adds a fixed delay to try and give the tracker a chance to recover.
    """
    not_working_trackers = (t for t in client.torrents_trackers(torrent_hash=torrent.hash) if t.status in {TrackerStatus.NOT_WORKING, TrackerStatus.UPDATING})
    for not_working_tracker in not_working_trackers:
        if "too many requests" in not_working_tracker.msg.lower():
            logger.info("Tracker [%s] has reported [Too Many Requests], so adding a delay of [%ds] before trying again", not_working_tracker.url, TOO_MANY_REQUESTS_DELAY)
            interruptible_sleep(TOO_MANY_REQUESTS_DELAY, stop_event)
            return True
    return False


def _reannounce(client: Client, torrent: TorrentDictionary) -> bool:
    if any(tracker.status == TrackerStatus.WORKING for tracker in client.torrents_trackers(torrent_hash=torrent.hash)):
        logger.info("Skipping reannounce for torrent [%s], as at least one tracker is already working", torrent.name)
        return True
    client.torrents_reannounce(torrent_hashes=torrent.hash)
    trackers = [tracker for tracker in client.torrents_trackers(torrent_hash=torrent.hash) if tracker.status != TrackerStatus.DISABLED]
    if logger.isEnabledFor(logging.DEBUG):
        for tracker in trackers:
            if tracker.msg:
                logger.debug("Tracker [%s] has status [%s] and message [%s]", tracker.url, TrackerStatus(tracker.status).display, tracker.msg)
            else:
                logger.debug("Tracker [%s] has status [%s]", tracker.url, TrackerStatus(tracker.status).display)

    return any(tracker.status == TrackerStatus.WORKING for tracker in trackers)


def _resume_torrents(client: Client, paused_torrent_hashes: list[str] | set[str]) -> None:
    if paused_torrent_hashes:
        logger.info("Resuming [%d] previously paused torrents", len(paused_torrent_hashes))
        found_paused_torrent_hashes = {t.hash for t in (client.torrents_info(torrent_hashes=paused_torrent_hashes) or [])}
        missing_torrent_hashes = set(paused_torrent_hashes) - found_paused_torrent_hashes
        for missing_torrent_hash in missing_torrent_hashes:
            logger.warning("No torrent found with hash [%s], so it cannot be resumed", missing_torrent_hash)
        client.torrents_resume(torrent_hashes=found_paused_torrent_hashes)
    else:
        logger.info("No paused torrents to resume")


def _get_torrent(client: Client, torrent_hash: str) -> TorrentDictionary | None:
    return next(iter(client.torrents_info(torrent_hashes=torrent_hash)), None)


def _is_torrent_manually_paused(torrent: TorrentDictionary):
    return torrent.state_enum.is_paused and torrent.hash not in load_all_paused_torrent_hashes()


def _check_for_interrupt(stop_event: threading.Event, torrent_name: str) -> None:
    if stop_event and stop_event.is_set():
        raise TaskInterrupted(f"Cancellation request received for torrent [{torrent_name}], so stopping race")
