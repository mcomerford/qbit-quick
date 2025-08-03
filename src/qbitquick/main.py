import logging.config
import sys

from qbitquick.cli import app
from qbitquick.config import APP_NAME
from qbitquick.error_handler import setup_uncaught_exception_handler
from qbitquick.log_config.fallback_logger import setup_fallback_logging
from qbitquick.log_config.logging_config import LOGGING_CONFIG
from qbitquick.task_manager import TaskInterrupted

logger = logging.getLogger(__name__)
setup_fallback_logging()
setup_uncaught_exception_handler()
logging.config.dictConfig(LOGGING_CONFIG)

def main():
    # noinspection PyBroadException
    try:
        logger.info("%s called with arguments: %s", APP_NAME, sys.argv[1:])
        app(prog_name=APP_NAME)
    except TaskInterrupted as e:
        logger.info(e.message)
        exit(0)
    except (OSError, ValueError) as e:
        logger.error(e)
        exit(1)
    except Exception:
        logger.exception("Unhandled exception")
        sys.exit(2)

if __name__ == "__main__":
    main()
