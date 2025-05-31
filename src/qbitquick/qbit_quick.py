import argparse
import json
import logging.config
import os
import sqlite3
import subprocess
import sys
import time
from importlib import resources

import jsonschema
import platformdirs
from jsonschema import ValidationError, FormatChecker
from qbittorrentapi import Client, TrackerStatus
from qbittorrentapi.torrents import TorrentInfoList

from qbitquick.config import APP_NAME, TOO_MANY_REQUESTS_DELAY
from qbitquick.database.database_handler import load_all_paused_torrent_hashes, delete_torrent, \
    save_torrent_hashes_to_pause, load_torrents_to_unpause, clear_db, print_db
from qbitquick.error_handler import setup_uncaught_exception_handler
from qbitquick.log_config.fallback_logger import setup_fallback_logging
from qbitquick.log_config.logging_config import LOGGING_CONFIG

logger = logging.getLogger(__name__)
setup_fallback_logging()
setup_uncaught_exception_handler()
logging.config.dictConfig(LOGGING_CONFIG)

unregistered_messages = ["unregistered", "stream truncated"]


def main():
    parser = argparse.ArgumentParser(description="qBittorrent racing tools")
    subparsers = parser.add_subparsers(dest="subparser_name")

    race_parser = subparsers.add_parser("race", help="race the provided torrent")
    race_parser.add_argument("torrent_hash", help="hash of the torrent to race")

    post_race_parser = subparsers.add_parser("post_race", help="run the post race steps for the provided torrent, "
                                                               "such as resuming torrents that were previously paused")
    post_race_parser.add_argument("torrent_hash", help="hash of the torrent that has finished racing")

    config_parser = subparsers.add_parser("config", help="perform actions related to the config")
    config_parser_group = config_parser.add_mutually_exclusive_group(required=True)
    config_parser_group.add_argument("--print", action="store_true", help="print the current config")
    config_parser_group.add_argument("--edit", action="store_true", help="edit the current config or create one if it does not exist")

    db_parser = subparsers.add_parser("db", help="perform actions related to the sqlite database")
    db_parser = db_parser.add_mutually_exclusive_group(required=True)
    db_parser.add_argument("--print", action="store_true", help="print the contents of the database")
    db_parser.add_argument("--clear", action="store_true", help="clear the database")

    args = parser.parse_args(args=None if sys.argv[1:] else ["--help"])
    logger.info("%s called with arguments: %s", APP_NAME, args)

    config_path = os.path.join(platformdirs.user_config_dir(appname=APP_NAME, appauthor=False), "config.json")
    if not os.path.exists(config_path):
        logger.info("config.json not found, so creating default")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        default_config_path = resources.files("qbitquick") / "resources" / "default_config.json"
        with default_config_path.open("rb") as src, open(config_path, "wb") as dst:
            dst.write(src.read())

        logger.info("Created default config.json at: %s", config_path)

    with open(config_path, "r") as f:
        logger.info("Loading config.json from: %s", config_path)
        try:
            config = json.loads(f.read())
        except json.decoder.JSONDecodeError as e:
            logger.error("Failed to load config.json: %s", e)
            return 1
        logger.debug("Loaded config: %s", config)

    config_schema_path = resources.files("qbitquick") / "resources" / "config_schema.json"
    with config_schema_path.open("r") as f:
        config_schema = json.loads(f.read())

    try:
        jsonschema.validate(instance=config, schema=config_schema, format_checker=FormatChecker())
    except ValidationError as e:
        logger.error("Invalid config: %s", e.message)
        return 1

    debug_logging = config["debug_logging"] if "debug_logging" in config else False
    update_log_level(debug_logging)
    logger.info("DEBUG level logging %s", "enabled" if debug_logging else "disabled")

    if args.subparser_name == "race":
        return race(config, args.torrent_hash)
    elif args.subparser_name == "post_race":
        return post_race(config, args.torrent_hash)
    elif args.subparser_name == "config":
        if args.print:
            return print_config(config, config_path)
        elif args.edit:
            return edit_config(config_path)
        return None
    elif args.subparser_name == "db":
        if args.print:
            return print_db()
        elif args.clear:
            return clear_db()
        return None
    return None


def connect(config):
    # Filter config to just the qBittorrent connection info
    client_params = {"host", "port", "username", "password"}
    conn_info = {k: v for k, v in config.items() if k in client_params}
    client = Client(**conn_info)
    client.auth_log_in() # This just checks that the connection and login is successful
    logger.info("Connected to qBittorrent successfully")

    logger.info("qBittorrent: %s", client.app.version)
    logger.info("qBittorrent Web API: %s", client.app.webapiVersion)
    if logger.isEnabledFor(logging.DEBUG):
        for k, v in client.app.build_info.items():
            logger.info("%s: %s", k, v)

    return client


def race(config, racing_torrent_hash):
    # noinspection PyBroadException
    try:
        client = connect(config)
    except Exception:
        logger.exception("Failed to connect to qBittorrent")
        return 1

    race_categories = config["race_categories"] if "race_categories" in config else []
    if not race_categories:
        logger.info("No race categories are set, so all torrents are eligible for racing")

    ignore_categories = config["ignore_categories"] if "ignore_categories" in config else []
    if ignore_categories:
        logger.info("Ignore categories %s", ignore_categories)

    max_reannounce = config["max_reannounce"] if "max_reannounce" in config else None
    if max_reannounce and max_reannounce > 0:
        logger.info("Maximum number of reannounce requests is set to [%d]", max_reannounce)
    else:
        max_reannounce = None
        logger.info("Maximum number of reannounce requests is set to [Unlimited]")

    reannounce_frequency = config["reannounce_frequency"] if "reannounce_frequency" in config else 5.0
    logger.info("Reannounce frequency set to [%.2f] seconds", reannounce_frequency)

    pausing = config["pausing"] if "pausing" in config else False
    logger.info("Pausing of torrents before racing is [%s]", "Enabled" if pausing else "Disabled")

    torrents = client.torrents_info()
    racing_torrent = next((t for t in torrents if t.hash == racing_torrent_hash), None)
    if not racing_torrent:
        logger.error("No torrent found with hash [%s]", racing_torrent_hash)
        return 1
    torrents.remove(racing_torrent)

    # Check the category on the race torrent
    if race_categories:
        if not racing_torrent.category:
            logger.info("Not racing torrent [%s], as no category is set. Valid race categories are: %s",
                        racing_torrent.name, race_categories)
            return 1
        if racing_torrent.category not in race_categories:
            logger.info("Not racing torrent [%s], as category [%s] is not in the list of racing categories %s",
                        racing_torrent.name, racing_torrent.category, race_categories)
            return 1

    # Remove any torrents with an ignored category
    if ignore_categories:
        ignored_torrents = TorrentInfoList([t for t in torrents if t.category in ignore_categories])
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Ignored torrents: %s", [t.name for t in ignored_torrents])
        logger.info("Ignoring %d torrents, as their category is one of %s", len(ignored_torrents),
                    ignore_categories)
        torrents = TorrentInfoList([t for t in torrents if t not in ignored_torrents])

    torrent_hashes_to_pause = set()
    if pausing:
        logger.info("Pausing is enabled, so checking which torrents to pause")

        ratio = config.get("ratio", 0)
        logger.info("Minimum ratio to be eligible for pausing is set to [%d]", ratio)

        if race_categories:
            logger.info("Valid race categories are %s", race_categories)
        else:
            logger.info("No race categories are set, so all torrents are eligible for pausing")

        for torrent in torrents:
            if torrent.state_enum.is_paused and torrent.hash not in load_all_paused_torrent_hashes():
                logger.info("Ignoring torrent [%s] as it is already paused", torrent.name)
                continue
            if not race_categories or not torrent.category or torrent.category not in race_categories:
                logger.info("Adding torrent [%s] to pause list, as category [%s] is not a valid race category",
                            torrent.name, torrent.category)
                torrent_hashes_to_pause.add(torrent.hash)
            elif torrent.ratio >= ratio:
                logger.info("Adding torrent [%s] to pause list as ratio [%f] >= [%f]",
                            torrent.name, torrent.ratio, ratio)
                torrent_hashes_to_pause.add(torrent.hash)
    else:
        logger.info("Pausing is disabled, so no torrents will be paused")

    # When a new torrent is added, the data will be checked first. Need to wait until this is done.
    # Can be improved if this is ever implemented: https://github.com/qbittorrent/qBittorrent/issues/9177
    while True:
        racing_torrent = get_torrent(client, racing_torrent_hash)
        if not racing_torrent:
            logger.error("No torrent found with hash [%s]", racing_torrent_hash)
            return 1
        if not racing_torrent.state_enum.is_checking:
            break
        logger.debug("Waiting while torrent [%s] is checking...", racing_torrent.name)
        time.sleep(0.1)

    if racing_torrent.state_enum.is_paused:
        logger.info("Not racing torrent [%s] as it is paused/stopped", racing_torrent.name)
        return 1
    elif racing_torrent.state_enum.is_complete:
        logger.info("Not racing torrent [%s] as it is already complete", racing_torrent.name)
        return 1

    try:
        save_torrent_hashes_to_pause(racing_torrent_hash, torrent_hashes_to_pause)
    except sqlite3.DatabaseError:
        logger.exception("Failed to save paused torrent [%s] to database", racing_torrent.name)
        return 1

    if torrent_hashes_to_pause:
        logger.info("Pausing [%d] torrents before racing", len(torrent_hashes_to_pause))
        client.torrents_pause(torrent_hashes=torrent_hashes_to_pause)

    # Continually reannounce until the torrent is available in the tracker
    if not reannounce_until_working(client, max_reannounce, reannounce_frequency, racing_torrent_hash):
        resume_torrents(client, torrent_hashes_to_pause)
        return 1

    logger.info("Racing complete for torrent [%s]", racing_torrent.name)
    return 0


def reannounce_until_working(client, max_reannounce, reannounce_frequency, torrent_hash):
    reannounce_count = 0
    while not max_reannounce or reannounce_count < max_reannounce:
        torrent = get_torrent(client, torrent_hash)
        if not torrent:
            logger.error("Aborting race, as torrent with hash [%s] no longer exists", torrent_hash)
            return False
        if torrent.state_enum.is_stopped:
            logger.error("Aborting race, as torrent [%s] has been stopped", torrent.name)
            return False

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
            time.sleep(reannounce_frequency)
            continue
        if handle_unregistered_torrent(client, torrent) or handle_too_many_requests(client, torrent):
            continue
        if reannounce(client, torrent):
            logger.info("Torrent [%s] has at least 1 working tracker", torrent.name)
            return True
        reannounce_count += 1
        logger.info("Sent reannounce [%s] of [%s] for torrent [%s]",
                    reannounce_count, max_reannounce if max_reannounce else "Unlimited", torrent.name)
        time.sleep(reannounce_frequency)

    torrent = get_torrent(client, torrent_hash)
    if torrent:
        logger.info("Giving up, as there are still no working trackers for torrent [%s]", torrent.name)
    else:
        logger.info("Giving up, as there are still no working trackers for torrent with hash [%s]", torrent_hash)
    return False


def handle_unregistered_torrent(client, torrent):
    """
    When a new torrent is added, the tracker may state that the torrent is unregistered. In this case,
    reannouncing won't help and the torrent has to be stopped and restarted. Forcing a recheck is an
    easy way to do this.
    """
    not_working_trackers = [tracker for tracker in client.torrents_trackers(torrent_hash=torrent.hash)
                            if tracker.status == TrackerStatus.NOT_WORKING]
    for not_working_tracker in not_working_trackers:
        tracker_msg = not_working_tracker.msg.lower()
        if any(msg in tracker_msg for msg in unregistered_messages):
            if torrent.progress == 0:
                logger.info("Torrent [%s] has been marked as [%s] in tracker [%s], so forcing a recheck",
                            torrent.name, not_working_tracker.msg, not_working_tracker.url)
                client.torrents_recheck(torrent_hashes=torrent.hash)
            else:
                logger.info("Torrent [%s] has been marked as [%s] in tracker [%s], so forcing a restart",
                            torrent.name, not_working_tracker.msg, not_working_tracker.url)
                client.torrents_stop(torrent_hashes=torrent.hash)
                client.torrents_start(torrent_hashes=torrent.hash)
            return True
    return False


def handle_too_many_requests(client, torrent):
    """
    If too many requests are sent in a short space of time, the tracker will block any further requests.
    It's not clear what the limit it is, but this adds a fixed delay to try and give the tracker a chance to recover.
    """
    not_working_trackers = (
        t for t in client.torrents_trackers(torrent_hash=torrent.hash)
        if t.status in {TrackerStatus.NOT_WORKING, TrackerStatus.UPDATING}
    )
    for not_working_tracker in not_working_trackers:
        if "too many requests" in not_working_tracker.msg.lower():
            logger.info("Tracker [%s] has reported [Too Many Requests], so adding a delay of [%ds] before trying again",
                        not_working_tracker.url, TOO_MANY_REQUESTS_DELAY)
            time.sleep(TOO_MANY_REQUESTS_DELAY)
            return True
    return False


def reannounce(client, torrent):
    if any(tracker.status == TrackerStatus.WORKING for tracker in client.torrents_trackers(torrent_hash=torrent.hash)):
        logger.info("Skipping reannounce for torrent [%s], as at least one tracker is already working",
                    torrent.name)
        return True
    client.torrents_reannounce(torrent_hashes=torrent.hash)
    trackers = [tracker for tracker in client.torrents_trackers(torrent_hash=torrent.hash) if
                tracker.status != TrackerStatus.DISABLED]
    if logger.isEnabledFor(logging.DEBUG):
        for tracker in trackers:
            if tracker.msg:
                logger.debug("Tracker [%s] has status [%s] and message [%s]",
                             tracker.url, TrackerStatus(tracker.status).display, tracker.msg)
            else:
                logger.debug("Tracker [%s] has status [%s]",
                             tracker.url, TrackerStatus(tracker.status).display)

    return any(tracker.status == TrackerStatus.WORKING for tracker in trackers)


def resume_torrents(client, paused_torrent_hashes):
    if paused_torrent_hashes:
        logger.info("Resuming [%d] previously paused torrents", len(paused_torrent_hashes))
        found_paused_torrent_hashes = {t.hash for t in (client.torrents_info(torrent_hashes=paused_torrent_hashes) or [])}
        missing_torrent_hashes = set(paused_torrent_hashes) - found_paused_torrent_hashes
        for missing_torrent_hash in missing_torrent_hashes:
            logger.warning("No torrent found with hash [%s], so it cannot be resumed", missing_torrent_hash)
        client.torrents_resume(torrent_hashes=found_paused_torrent_hashes)
    else:
        logger.info("No paused torrents to resume")


def post_race(config, torrent_hash):
    # noinspection PyBroadException
    try:
        client = connect(config)
    except Exception:
        logger.exception("Failed to connect to qBittorrent")
        return 1

    torrent = get_torrent(client, torrent_hash)
    if not torrent:
        logger.error("No torrent found with hash [%s], so no post race actions can be run", torrent_hash)
        return 1

    torrents_to_unpause = load_torrents_to_unpause(torrent_hash)
    resume_torrents(client, torrents_to_unpause)
    if delete_torrent(torrent_hash) > 0:
        logger.info("Deleted torrent [%s]", torrent.name)

    logger.info("Post race complete for torrent [%s]", torrent.name)
    return 0


def print_config(config, config_path):
    print("Config Path: " + config_path)
    print(json.dumps(config, indent=2))
    return 0


def edit_config(config_path):
    editor = os.environ.get("EDITOR", "vi")
    if sys.platform.startswith("win") and not os.environ.get("EDITOR"):
        editor = "notepad"
    # noinspection PyBroadException
    try:
        subprocess.run([editor, config_path])
    except Exception:
        logger.exception("Could not open file: %s", config_path)
        return 1
    return 0


def get_torrent(client, torrent_hash):
    return next(iter(client.torrents_info(torrent_hashes=torrent_hash)), None)


def update_log_level(debug_enabled):
    level = logging.DEBUG if debug_enabled else logging.INFO
    logging.getLogger().setLevel(level)

    for handler in logging.getLogger().handlers:
        handler.setLevel(level)


if __name__ == "__main__":
    sys.exit(main())