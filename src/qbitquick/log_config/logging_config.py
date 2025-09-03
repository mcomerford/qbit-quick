import os
from pathlib import Path

from platformdirs import user_state_path

from qbitquick.config import APP_NAME, QBQ_LOGS_DIR

default_logs_path = user_state_path(APP_NAME, appauthor=False) / "logs"
logs_path = Path(os.getenv(QBQ_LOGS_DIR, str(default_logs_path)))
log_file_path = logs_path / f"{APP_NAME}.log"

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s.%(msecs)03d %(threadName)s [%(levelname)s] %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S"
        },
        "simple": {
            "format": "%(levelname)s: %(message)s"
        }
    },
    "handlers": {
        "console": {
            "level": "ERROR",
            "class": "logging.StreamHandler",
            "formatter": "simple",
            "stream": "ext://sys.stderr"
        },
        "file": {
            "level": "INFO",
            "class": "qbitquick.log_config.safe_handler.SafeTimedRotatingFileHandler",
            "when": "midnight",
            "backupCount": 7,
            "formatter": "default",
            "filename": str(log_file_path)
        }
    },
    "root": {
        "level": "DEBUG",
        "handlers": ["console", "file"]
    }
}

