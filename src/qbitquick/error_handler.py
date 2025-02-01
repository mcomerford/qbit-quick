import sys
import logging

logger = logging.getLogger(__name__)

def log_uncaught_exceptions(exc_type, exc_value, exc_traceback):
    logger.critical("Uncaught Exception", exc_info=(exc_type, exc_value, exc_traceback))

def setup_uncaught_exception_handler():
    sys.excepthook = log_uncaught_exceptions