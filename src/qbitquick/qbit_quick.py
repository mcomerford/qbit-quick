import argparse
import inspect
import json
import logging.config
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from importlib import resources

import platformdirs
from qbittorrentapi import Client, TrackerStatus
from qbittorrentapi.torrents import TorrentInfoList
from tabulate import tabulate

from qbitquick.error_handler import setup_uncaught_exception_handler
from qbitquick.log_config.fallback_logger import setup_fallback_logging
from qbitquick.log_config.logging_loader import load_logging_config

APP_NAME = 'qbit-quick'
TOO_MANY_REQUESTS_DELAY = 10

logger = logging.getLogger(__name__)
setup_fallback_logging()
setup_uncaught_exception_handler()
load_logging_config()


def main():
    parser = argparse.ArgumentParser(description='qBittorrent racing tools')
    subparsers = parser.add_subparsers(dest='subparser_name')

    race_parser = subparsers.add_parser('race', help='race the provided torrent')
    race_parser.add_argument('torrent_hash', help='hash of the torrent to race')

    post_race_parser = subparsers.add_parser('post_race', help='run the post race steps for the provided torrent, '
                                                               'such as resuming torrents that were previously paused')
    post_race_parser.add_argument('torrent_hash', help='hash of the torrent that has finished racing')

    config_parser = subparsers.add_parser('config', help='perform actions related to the config')
    config_parser_group = config_parser.add_mutually_exclusive_group(required=True)
    config_parser_group.add_argument('--print', action='store_true', help='print the current config')
    config_parser_group.add_argument('--edit', action='store_true', help='edit the current config or create one if it does not exist')

    db_parser = subparsers.add_parser('db', help='perform actions related to the sqlite database')
    db_parser = db_parser.add_mutually_exclusive_group(required=True)
    db_parser.add_argument('--print', action='store_true', help='print the contents of the database')
    db_parser.add_argument('--clear', action='store_true', help='clear the database')

    args = parser.parse_args(args=None if sys.argv[1:] else ['--help'])
    logger.info("%s called with arguments: %s", APP_NAME, args)

    config_path = os.path.join(platformdirs.user_config_dir(appname=APP_NAME, appauthor=False), 'config.json')
    if not os.path.exists(config_path):
        logger.info('config.json not found, so creating default')
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        script_dir = os.path.dirname(os.path.realpath(__file__))
        default_config_path = os.path.join(script_dir, 'default_config.json')
        shutil.copyfile(default_config_path, config_path)
        logger.info('Created default config.json at: %s', config_path)

    with open(config_path) as f:
        logger.info('Loading config.json from: %s', config_path)
        config = json.loads(f.read())
        logger.debug('Loaded config: %s', config)

    if args.subparser_name == 'race':
        race(config, args.torrent_hash)
    elif args.subparser_name == 'post_race':
        post_race(config, args.torrent_hash)
    elif args.subparser_name == 'config':
        if args.print:
            print_config(config, config_path)
        elif args.edit:
            edit_config(config_path)
    elif args.subparser_name == 'db':
        if args.print:
            print_db()
        elif args.clear:
            clear_db()


def connect(config):
    # Filter config to just the qBittorrent connection info
    conn_info = {k: v for k, v in config.items() if k in [p.name for p in inspect.signature(Client).parameters.values()]}
    qbt_client = Client(**conn_info)
    logger.info('Connected to qBittorrent successfully')

    logger.info('qBittorrent: %s', qbt_client.app.version)
    logger.info('qBittorrent Web API: %s', qbt_client.app.webapiVersion)
    for k, v in qbt_client.app.build_info.items():
        logger.info('%s: %s', k, v)

    return qbt_client


def race(config, torrent_hash):
    client = connect(config)

    race_categories = config['race_categories'] if 'race_categories' in config else []
    if not race_categories:
        logger.info('No race categories are set, so all torrents are eligible for racing')

    ignore_categories = config['ignore_categories'] if 'ignore_categories' in config else []
    if ignore_categories:
        logger.info('Ignore categories %s', ignore_categories)

    max_reannounce = config['max_reannounce'] if 'max_reannounce' in config else None
    if max_reannounce and max_reannounce > 0:
        logger.info('Maximum number of reannounce requests is set to [%d]', max_reannounce)
    else:
        max_reannounce = None
        logger.info('Maximum number of reannounce requests is set to [Unlimited]')

    reannounce_frequency = config['reannounce_frequency'] if 'reannounce_frequency' in config else 0.5
    logger.info('Reannounce frequency set to [%.2f] seconds', reannounce_frequency)

    pausing = config['pausing'] if 'pausing' in config else False
    logger.info('Pausing of torrents before racing is [%s]', 'Enabled' if pausing else 'Disabled')

    torrents = client.torrents_info()
    race_torrent = next(filter(lambda x: x.hash == torrent_hash, torrents), None)
    if not race_torrent:
        logger.error('No torrent found with hash [%s]', torrent_hash)
        return
    torrents.remove(race_torrent)

    # Check the category on the race torrent
    if race_categories:
        if not race_torrent.category:
            logger.info('Not racing torrent [%s], as no category is set. Valid race categories are: %s',
                        race_torrent.name, race_categories)
            return
        if race_torrent.category not in race_categories:
            logger.info('Not racing torrent [%s], as category [%s] is not in the list of racing categories %s',
                        race_torrent.name, race_torrent.category, race_categories)
            return

    # Remove any torrents with an ignored category
    if ignore_categories:
        ignored_torrents = TorrentInfoList(filter(lambda x: x.category in ignore_categories, torrents))
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug('Ignored torrents: %s', [x.name for x in ignored_torrents])
        logger.info('Ignoring %d torrents, as their category is one of %s', len(ignored_torrents),
                    ignore_categories)
        torrents = TorrentInfoList(filter(lambda x: x not in ignored_torrents, torrents))

    torrent_hashes_to_pause = []
    if pausing:
        logger.info('Pausing is enabled, so checking which torrents to pause')

        ratio = config.get('ratio', 0)
        logger.info('Minimum ratio to be eligible for pausing is set to [%d]', ratio)

        if race_categories:
            logger.info('Valid race categories are %s', race_categories)
        else:
            logger.info('No race categories are set, so all torrents are eligible for pausing')

        for torrent in torrents:
            if torrent.state_enum.is_paused:
                logger.debug('Skipping torrent [%s] as it is already paused', torrent.name)
                continue

            if not race_categories or not torrent.category or torrent.category not in race_categories:
                logger.info('Adding torrent [%s] to pause list, as category [%s] is not a valid race category',
                            torrent.name, torrent.category)
                torrent_hashes_to_pause.append(torrent.hash)
            elif torrent.ratio >= ratio:
                logger.info('Adding torrent [%s] to pause list as ratio [%f] >= [%f]',
                            torrent.name, torrent.ratio, ratio)
                torrent_hashes_to_pause.append(torrent.hash)
    else:
        logger.info('Pausing is disabled, so no torrents will be paused')

    # When a new torrent is added, the data will be checked first. Need to wait until this is done.
    # Can be improved if this is ever implemented: https://github.com/qbittorrent/qBittorrent/issues/9177
    while True:
        race_torrent = next(iter(client.torrents_info(torrent_hashes=torrent_hash)), None)
        if not race_torrent:
            logger.error('No torrent found with hash [%s]', torrent_hash)
            return
        if not race_torrent.state_enum.is_checking:
            break
        logger.debug('Waiting while torrent [%s] is checking...', race_torrent.name)
        time.sleep(0.1)

    if race_torrent.state_enum.is_paused:
        logger.info('Not racing torrent [%s] as it is paused/stopped', race_torrent.name)
        return
    elif race_torrent.state_enum.is_complete:
        logger.info('Not racing torrent [%s] as it is already complete', race_torrent.name)
        # return

    if torrent_hashes_to_pause:
        logger.info('Pausing [%d] torrents before racing', len(torrent_hashes_to_pause))
        client.torrents_pause(torrent_hashes=torrent_hashes_to_pause)

    db_path = os.path.join(platformdirs.user_config_dir(appname=APP_NAME, appauthor=False), 'race.sqlite')
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        with closing(conn.cursor()) as cur:
            ddl_file = resources.files('qbitquick') / 'race.ddl'
            with ddl_file.open('r') as f:
                cur.executescript(f.read())
            cur.execute(
                '''
                INSERT INTO race (racing_torrent_hash, paused_torrent_hashes, has_finished)
                VALUES (?, ?, ?)
                ON CONFLICT(racing_torrent_hash) 
                DO UPDATE SET 
                    paused_torrent_hashes = excluded.paused_torrent_hashes,
                    has_finished = excluded.has_finished;
                ''',
                (torrent_hash, json.dumps(torrent_hashes_to_pause), 0),
            )
            conn.commit()

    # Continually reannounce until the torrent is available in the tracker
    if not reannounce_until_working(client, max_reannounce, reannounce_frequency, torrent_hash):
        resume_torrents(client, torrent_hashes_to_pause)
        return

    logger.info('Racing complete for torrent [%s]', race_torrent.name)


def reannounce_until_working(client, max_reannounce, reannounce_frequency, torrent_hash):
    reannounce_count = 1
    while not max_reannounce or reannounce_count < max_reannounce:
        race_torrent = next(iter(client.torrents_info(torrent_hashes=torrent_hash)), None)
        if not race_torrent:
            logger.error('Aborting race, as torrent with hash [%s] no longer exists', torrent_hash)
            return False
        if race_torrent.state_enum.is_stopped:
            logger.error('Aborting race, as torrent [%s] has been stopped', race_torrent.name)
            return False
        if any(tracker.status == TrackerStatus.UPDATING for tracker in client.torrents_trackers(torrent_hash=torrent_hash)):
            logger.debug("Waiting on torrent [%s] while trackers are updating...", race_torrent.name)
            time.sleep(reannounce_frequency)
            continue
        if handle_unregistered_torrent(client, race_torrent) or handle_too_many_requests(client, race_torrent):
            continue
        if reannounce(client, race_torrent):
            logger.info('Torrent [%s] now has at least 1 working tracker', race_torrent.name)
            return True
        logger.info('Sent reannounce [%s] of [%s] for torrent [%s]',
                    reannounce_count, max_reannounce if max_reannounce else 'Unlimited', race_torrent.name)
        time.sleep(reannounce_frequency)
        reannounce_count += 1

    race_torrent = next(iter(client.torrents_info(torrent_hashes=torrent_hash)), None)
    if race_torrent:
        logger.info('Giving up, as there are still no working trackers for torrent [%s]', race_torrent.name)
    else:
        logger.info('Giving up, as there are still no working trackers for torrent with hash [%s]', torrent_hash)
    return False


def handle_unregistered_torrent(client, race_torrent):
    """
    When a new torrent is added, the tracker may state that the torrent is unregistered. In this case,
    reannouncing won't help and the torrent has to be stopped and restarted. Forcing a recheck is an
    easy way to do this.
    """
    torrent_hash = race_torrent.hash
    not_working_trackers = [x for x in client.torrents_trackers(torrent_hash=torrent_hash) if x.status == TrackerStatus.NOT_WORKING]
    for not_working_tracker in not_working_trackers:
        if 'unregistered' in not_working_tracker.msg.lower():
            if race_torrent.progress == 0:
                logger.info('Torrent [%s] has been marked as unregistered in tracker [%s], so forcing a recheck',
                            race_torrent.name, not_working_tracker.url)
                client.torrents_recheck(torrent_hashes=torrent_hash)
            else:
                logger.info('Torrent [%s] has been marked as unregistered in tracker [%s], so forcing a restart',
                            race_torrent.name, not_working_tracker.url)
                client.torrents_stop(torrent_hashes=torrent_hash)
                client.torrents_start(torrent_hashes=torrent_hash)
            time.sleep(1)
            return True
    return False


def handle_too_many_requests(client, race_torrent):
    """
    If too many requests are sent in a short space of time, the tracker will block any further requests.
    It's not clear what the limit it is, but this adds a delay to try and give the tracker a chance to recover.
    """
    torrent_hash = race_torrent.hash
    not_working_trackers = [x for x in client.torrents_trackers(torrent_hash=torrent_hash) if x.status in [TrackerStatus.NOT_WORKING, TrackerStatus.UPDATING]]
    for not_working_tracker in not_working_trackers:
        if 'too many requests' in not_working_tracker.msg.lower():
            logger.info('Tracker [%s] has reported [Too Many Requests], so adding a delay of [%ds] before trying again',
                        not_working_tracker.url, TOO_MANY_REQUESTS_DELAY)
            time.sleep(TOO_MANY_REQUESTS_DELAY)
            return True
    return False


def abort_if_stopped(client, torrent_hash):
    race_torrent = next(iter(client.torrents_info(torrent_hashes=torrent_hash)), None)
    return not race_torrent or race_torrent.state_enum.is_stopped


def resume_torrents(client, paused_torrent_hashes):
    logger.info('Resuming [%d] previously paused torrents', len(paused_torrent_hashes))
    found_paused_torrent_hashes = [x.hash for x in client.torrents_info(torrent_hashes=paused_torrent_hashes)]
    missing_torrent_hashes = set(paused_torrent_hashes) - set(found_paused_torrent_hashes)
    for missing_torrent_hash in missing_torrent_hashes:
        logger.error('No torrent found with hash [%s], so it cannot be resumed', missing_torrent_hash)
    client.torrents_resume(torrent_hashes=found_paused_torrent_hashes)


def reannounce(client, race_torrent):
    torrent_hash = race_torrent.hash
    if any(tracker.status == TrackerStatus.WORKING for tracker in client.torrents_trackers(torrent_hash=torrent_hash)):
        logger.info('Skipping reannounce for torrent [%s], as at least one tracker is already working',
                    race_torrent.name)
        return True
    client.torrents_reannounce(torrent_hashes=torrent_hash)
    trackers = [tracker for tracker in client.torrents_trackers(torrent_hash=torrent_hash) if tracker.status != TrackerStatus.DISABLED]
    if logger.isEnabledFor(logging.DEBUG):
        for tracker in trackers:
            if tracker.msg:
                logger.debug('Tracker [%s] has status [%s] and message [%s]',
                             tracker.url, TrackerStatus(tracker.status).display, tracker.msg)
            else:
                logger.debug('Tracker [%s] has status [%s]',
                             tracker.url, TrackerStatus(tracker.status).display)

    return any(tracker.status == TrackerStatus.WORKING for tracker in trackers)


def post_race(config, torrent_hash):
    client = connect(config)
    db_path = os.path.join(platformdirs.user_config_dir(appname=APP_NAME, appauthor=False), 'race.sqlite')
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        with closing(conn.cursor()) as cur:
            cur.execute('UPDATE race SET has_finished = 1 WHERE racing_torrent_hash = ?', (torrent_hash,))
            rows = cur.execute('SELECT * FROM race ORDER BY id DESC').fetchall()
            for row in rows:
                if row['has_finished']:
                    resume_torrents(client, json.loads(row['paused_torrent_hashes']))
                    cur.execute('DELETE FROM race WHERE id = ?', (row['id'],))
                else:
                    break
            conn.commit()

    race_torrent = next(iter(client.torrents_info(torrent_hashes=torrent_hash)), None)
    if race_torrent:
        logger.info('Post race complete for torrent [%s]', race_torrent.name)
    else:
        logger.info('Post race complete for torrent with hash [%s]', torrent_hash)


def print_config(config, config_path):
    print('Config Path: ' + config_path)
    print(json.dumps(config, indent=2))


def edit_config(config_path):
    editor = os.environ.get('EDITOR', 'vi')
    if sys.platform.startswith('win') and not os.environ.get('EDITOR'):
        editor = 'notepad'
    try:
        subprocess.run([editor, config_path])
    except Exception as e:
        logger.error('Error: Could not open file: %s', config_path, e)


def print_db():
    db_path = os.path.join(platformdirs.user_config_dir(appname=APP_NAME, appauthor=False), 'race.sqlite')
    logger.info("Database path: %s", db_path)
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        cur.execute('SELECT * FROM race')
        rows = cur.fetchall()
        headers = [desc[0] for desc in cur.description]  # Get column names

        print(tabulate(rows, headers=headers, tablefmt="grid"))


def clear_db():
    db_path = os.path.join(platformdirs.user_config_dir(appname=APP_NAME, appauthor=False), 'race.sqlite')
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        # noinspection SqlWithoutWhere
        cur.execute('DELETE FROM race')
        conn.commit()
        logger.info('Database cleared of all rows')

if __name__ == '__main__':
    main()