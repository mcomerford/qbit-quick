import argparse
from argparse import ArgumentParser


def build_parser() -> ArgumentParser:
    parser = argparse.ArgumentParser(description="qBittorrent racing tools")
    subparsers = parser.add_subparsers(dest="subparser_name")

    server_parser = subparsers.add_parser("server", help="run in server mode")
    server_parser.add_argument("--port", type=int, default=8081, help="server port")

    race_parser = subparsers.add_parser("race", help="race the provided torrent")
    race_parser.add_argument("torrent_hash", help="hash of the torrent to race")

    post_race_parser = subparsers.add_parser("post-race", help="run the post race steps for the provided torrent, such as resuming torrents that were previously paused")
    post_race_parser.add_argument("torrent_hash", help="hash of the torrent that has finished racing")

    config_parser = subparsers.add_parser("config", help="perform actions related to the config")
    config_parser_group = config_parser.add_mutually_exclusive_group(required=True)
    config_parser_group.add_argument("--print", action="store_true", help="print the current config")
    config_parser_group.add_argument("--edit", action="store_true", help="edit the current config or create one if it does not exist")

    db_parser = subparsers.add_parser("db", help="perform actions related to the sqlite database")
    db_parser_group = db_parser.add_mutually_exclusive_group(required=True)
    db_parser_group.add_argument("--print", action="store_true", help="print the contents of the database")
    db_parser_group.add_argument("--clear", action="store_true", help="clear the database")
    db_parser_group.add_argument("--delete", metavar="torrent_hash", help="deletes the specified entry from the database")

    return parser
