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
        }
    },
    "handlers": {
        "console": {
            "level": "INFO",
            "class": "logging.StreamHandler",
            "formatter": "default",
            "stream": "ext://sys.stdout"
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
        "level": "INFO",
        "handlers": ["console", "file"]
    },
    "loggers": {
        "uvicorn": {
            "level": "INFO",
            "handlers": ["console", "file"],
            "propagate": False
        },
        "uvicorn.error": {
            "level": "INFO",
            "handlers": ["console", "file"],
            "propagate": False
        },
        "uvicorn.access": {
            "level": "INFO",
            "handlers": ["console", "file"],
            "propagate": False
        }
    }
}

