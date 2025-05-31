from pathlib import Path

from platformdirs import user_state_dir

from qbitquick.config import APP_NAME

log_filename = Path(user_state_dir(APP_NAME, appauthor=False)) / "logs" / f"{APP_NAME}.log"
LOGGING_CONFIG = {
    "version": 1,
    "formatters": {
        "default": {
            "format": "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
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
            "filename": str(log_filename)
        }
    },
    "loggers": {
        "root": {
            "level": "INFO",
            "handlers": ["console", "file"]
        }
    },
    "disable_existing_loggers": False
}
