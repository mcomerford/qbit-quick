import logging
import sys
import types

logger = logging.getLogger(__name__)


def log_uncaught_exceptions(exc_type: type[BaseException], exc_value: BaseException, exc_traceback: types.TracebackType | None) -> None:
    logger.critical("Uncaught Exception", exc_info=(exc_type, exc_value, exc_traceback))


def setup_uncaught_exception_handler() -> None:
    sys.excepthook = log_uncaught_exceptions
