import logging.config
import signal
import sys
import threading
import time

from qbitquick.argument_parser import build_parser
from qbitquick.config import APP_NAME, load_config
from qbitquick.database.database_handler import clear_db, delete_torrent, print_db
from qbitquick.error_handler import setup_uncaught_exception_handler
from qbitquick.handlers import edit_config, post_race, print_config, race, start_server
from qbitquick.log_config.fallback_logger import setup_fallback_logging
from qbitquick.log_config.logging_config import LOGGING_CONFIG
from qbitquick.server import create_app
from qbitquick.task_manager import TaskInterrupted

logger = logging.getLogger(__name__)
setup_fallback_logging()
setup_uncaught_exception_handler()
logging.config.dictConfig(LOGGING_CONFIG)

parser = build_parser()


def main(args: list[str] | None = None) -> int | None:
    cli_args = args if args is not None else (sys.argv[1:] or ["--help"])
    parsed_args = parser.parse_args(cli_args)
    logger.info("%s called with arguments: %s", APP_NAME, parsed_args)

    # Running in server mode
    config_file_path, config = load_config()
    if parsed_args.subparser_name == "server":
        app = create_app()
        start_server(app, parsed_args.port)
        return 0

    # Running in command line mode
    stop_event = _setup_cli_shutdown_hook()
    if parsed_args.subparser_name == "race":
        return race(config, parsed_args.torrent_hash, stop_event)
    elif parsed_args.subparser_name == "post-race":
        return post_race(config, parsed_args.torrent_hash)
    elif parsed_args.subparser_name == "config":
        if parsed_args.print:
            return print_config(str(config_file_path), config)
        elif parsed_args.edit:
            return edit_config(str(config_file_path))
    elif parsed_args.subparser_name == "db":
        if parsed_args.print:
            return print_db()
        elif parsed_args.clear:
            confirm = input("This will delete ALL entries from the database. Are you sure? (y/n)")
            if confirm.lower() == "y":
                return clear_db()
            else:
                return 0
        elif parsed_args.delete:
            return delete_torrent(parsed_args.delete)
    # Shouldn't be possible to reach here, as the arg parser would fail first
    raise ValueError(f"Unknown subcommand {parsed_args.subparser_name}")


def _setup_cli_shutdown_hook() -> threading.Event:
    stop_event = threading.Event()

    def handle_sigint(_sig, _frame):
        logger.warning("Interrupted via Ctrl+C, signalling shutdown...")
        stop_event.set()
        time.sleep(1)
        sys.exit(1)  # Forcefully exit if the process didn't stop in time

    signal.signal(signal.SIGINT, handle_sigint)
    return stop_event


if __name__ == "__main__":
    # noinspection PyBroadException
    try:
        sys.exit(main())
    except TaskInterrupted as e:
        logger.info(e.message)
        exit(0)
    except Exception:
        logger.exception("Unhandled exception")
        sys.exit(2)
