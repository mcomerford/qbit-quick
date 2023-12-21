import argparse
import inspect
import json
import logging.config
import os
import shutil
import sqlite3
import sys
import time
from contextlib import closing
from importlib import resources

import platformdirs
from qbittorrentapi import Client, TrackerStatus
from qbittorrentapi.torrents import TorrentInfoList

from qbitquick.logging_config import LOGGING_CONFIG

APP_NAME = 'qbit-quick'

logger = logging.getLogger(__name__)


def main():
    logging.config.dictConfig(LOGGING_CONFIG)

    parser = argparse.ArgumentParser(description='qBittorrent racing tools')
    subparsers = parser.add_subparsers(dest='subparser_name')

    race_parser = subparsers.add_parser('race', help='race the provided torrent')
    race_parser.add_argument('torrent_hash', help='hash of the torrent to race')

    post_race_parser = subparsers.add_parser('post_race', help='run the post race steps for the provided torrent, '
                                                               'such as resuming torrents that were previously paused')
    post_race_parser.add_argument('torrent_hash', help='hash of the torrent that has finished racing')

    args = parser.parse_args(args=None if sys.argv[1:] else ['--help'])

    config_path = os.path.join(platformdirs.user_config_dir(appname=APP_NAME, appauthor=False), 'config.json')
    if not os.path.exists(config_path):
        logger.info('config.json not found so creating default')
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        shutil.copyfile('./default_config.json', config_path)
        logger.info('Created default config.json at: %s', config_path)

    with open(config_path) as f:
        logger.info('Loading config.json from: %s', config_path)
        config = json.loads(f.read())
        logger.debug('Loaded config: %s', config)

    if args.subparser_name == 'race':
        race(config, args.torrent_hash)
    elif args.subparser_name == 'post_race':
        post_race(config, args.torrent_hash)


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
        logger.info('No racing categories are set, so all torrents are eligible for racing')

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
    logger.info('Pausing of torrents before racing is [%s]', pausing)

    torrents = client.torrents_info()
    race_torrent = next(filter(lambda x: x.hash == torrent_hash, torrents), None)
    if not race_torrent:
        logger.error('No torrent found with hash [%s]', torrent_hash)
        return False
    torrents.remove(race_torrent)

    # Check the category on the race torrent
    if race_categories:
        if not race_torrent.category:
            logger.info('Not racing torrent [%s], as no category is set. Valid race categories are: %s',
                        race_torrent.name, race_categories)
            return False
        if race_torrent.category not in race_categories:
            logger.info('Not racing torrent [%s], as category [%s] is not in the list of racing categories %s',
                        race_torrent.name, race_torrent.category, race_categories)
            return False

    # Remove any torrents with an ignored category
    if ignore_categories:
        ignored_torrents = TorrentInfoList(filter(lambda x: x.category in ignore_categories, torrents))
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug('Ignored torrents: %s', [x.name for x in ignored_torrents])
        logger.info('Ignoring %d torrents, as their category is one of %s', len(ignored_torrents),
                    ignore_categories)
        torrents = TorrentInfoList(filter(lambda x: x not in ignored_torrents, torrents))

    torrents_to_pause = []
    if pausing:
        logger.info('Pausing is enabled, so checking which torrents to pause')
        if race_categories:
            logger.info('Valid race categories are %s', race_categories)
            for torrent in torrents:
                if torrent.state_enum.is_paused:
                    logger.debug('Skipping torrent [%s] as it is already paused', torrent.name)
                    continue
                if not torrent.category:
                    logger.debug('Adding torrent [%s] to pause list, as no race category is set', torrent.name)
                    torrents_to_pause.append(torrent)
                elif torrent.category not in race_categories:
                    logger.debug('Adding torrent [%s] to pause list, as category [%s] is not a valid race category',
                                 torrent.name, torrent.category, race_categories)
                    torrents_to_pause.append(torrent)
                elif torrent.ratio >= config.ratio:
                    logger.debug('Adding torrent [%s] to pause list as ratio [%f] >= [%f]',
                                 torrent.name, torrent.ratio, config.ratio)
                    torrents_to_pause.append(torrent)

        if torrents_to_pause:
            logger.info('Pausing %d torrents before racing', len(torrents_to_pause))
            client.torrents_pause(torrent_hashes=[x.hash for x in torrents_to_pause])
    else:
        logger.info('Pausing is disabled, so no torrents will be paused')

    # When a new torrent is added, the data will be checked first. Need to wait until this is done.
    # Can be improved if this is ever implemented: https://github.com/qbittorrent/qBittorrent/issues/9177
    while True:
        race_torrent = client.torrents_info(torrent_hashes=torrent_hash)[0]
        if not race_torrent.state_enum.is_checking:
            break
        logger.debug('Waiting while torrent [%s] is checking...', race_torrent.name)
        time.sleep(0.1)

    if race_torrent.state_enum.is_paused:
        logger.info('Not racing torrent [%s] as it is paused', race_torrent.name)
        if pausing:
            resume_torrents(client, torrents_to_pause)
        return

    data_path = os.path.join(platformdirs.user_config_dir(appname=APP_NAME, appauthor=False), 'race.sqlite')
    with closing(sqlite3.connect(data_path, autocommit=False)) as conn:
        conn.row_factory = sqlite3.Row
        with closing(conn.cursor()) as cur:
            ddl_file = resources.files('qbitquick') / 'race.ddl'
            with ddl_file.open('r') as f:
                cur.executescript(f.read())
            cur.execute('INSERT INTO race(racing_torrent_hash, paused_torrent_hashes, has_finished) VALUES (?, ?, 0)',
                        (torrent_hash, json.dumps(torrents_to_pause),))
            conn.commit()

    # Continually reannounce until the torrent is available in the tracker
    if max_reannounce:
        for reannounce_count in range(1, max_reannounce):
            logger.info('Sending reannounce [%d] of [%d]', reannounce_count, max_reannounce)
            if reannounce(client, torrent_hash):
                logger.info('Reannounce was successful for at least 1 tracker')
                break
            time.sleep(reannounce_frequency)
        logger.info('Giving up, as there are still no working trackers')
        resume_torrents(client, torrents_to_pause)
    else:
        reannounce_count = 0
        while True:
            logger.info('Sending reannounce [%d]', reannounce_count := reannounce_count + 1)
            if reannounce(client, torrent_hash):
                logger.info('Reannounce was successful for at least 1 tracker')
                break
            time.sleep(reannounce_frequency)

    logger.info('Racing complete for torrent: %s', race_torrent.name)


def resume_torrents(client, paused_torrent_hashes):
    logger.info('Resuming [%d] previously paused torrents', len(paused_torrent_hashes))
    client.torrents_resume(torrent_hashes=paused_torrent_hashes)


def reannounce(client, torrent_hash):
    client.torrents_reannounce(torrent_hashes=torrent_hash)
    trackers = [x for x in client.torrents_trackers(torrent_hash=torrent_hash) if x.status != TrackerStatus.DISABLED]
    if logger.isEnabledFor(logging.DEBUG):
        for tracker in trackers:
            logger.debug('Tracker status for [%s] is [%s]', tracker.url, TrackerStatus(tracker.status).display)

    return any(x.status == TrackerStatus.WORKING for x in trackers)


def post_race(config, torrent_hash):
    client = connect(config)
    data_path = os.path.join(platformdirs.user_config_dir(appname=APP_NAME, appauthor=False), 'race.sqlite')
    with closing(sqlite3.connect(data_path, autocommit=False)) as conn:
        conn.row_factory = sqlite3.Row
        with closing(conn.cursor()) as cur:
            cur.execute('UPDATE race SET has_finished = 1 WHERE racing_torrent_hash = ?', (torrent_hash,))
            rows = cur.execute('SELECT * FROM race ORDER BY id DESC').fetchall()
            for row in rows:
                if row['has_finished']:
                    resume_torrents(client, row['paused_torrent_hashes'])
                    cur.execute('DELETE FROM race WHERE id = ?', (row['id']))
                else:
                    break
            conn.commit()

    logger.info('Post race complete for torrent: %s', torrent_hash.name)


if __name__ == '__main__':
    main()
