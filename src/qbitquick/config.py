import json
import logging
import os
import re
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema
import platformdirs
from jsonschema import FormatChecker, ValidationError


def _load_config_schema() -> dict[str, Any]:
    path = resources.files("qbitquick") / "resources" / "config_schema.json"
    with path.open("r") as f:
        return json.load(f)


# Constants
APP_NAME = "qbit-quick"
CONFIG_SCHEMA = _load_config_schema()
DATABASE_FILENAME = "paused_events.sqlite"
TOO_MANY_REQUESTS_DELAY = 10

# Environment variables
QBQ_LOGS_DIR = "QBQ_LOGS_DIR"
QBQ_CONFIG_DIR = "QBQ_CONFIG_DIR"
QBQ_STATE_DIR = "QBQ_STATE_DIR"

# Tracker response messages
UNREGISTERED_MESSAGES = ["unregistered", "stream truncated"]

# Regex for a duration e.g. 1w2d3h4m5s
DURATION_RE = re.compile(
    r"^(?:(?P<weeks>\d+)w)?"
    r"(?:(?P<days>\d+)d)?"
    r"(?:(?P<hours>\d+)h)?"
    r"(?:(?P<minutes>\d+)m)?"
    r"(?:(?P<seconds>\d+)s)?$"
)

format_checker = FormatChecker()

logger = logging.getLogger(__name__)


@format_checker.checks("duration")
def is_duration_format(value: str) -> bool:
    return bool(DURATION_RE.fullmatch(value))


def load_config() -> tuple[Path, dict[str, Any]]:
    default_config_dir = platformdirs.user_config_dir(APP_NAME, appauthor=False)
    config_path = Path(os.getenv(QBQ_CONFIG_DIR, default_config_dir))
    config_file_path = config_path / "config.json"
    if not config_file_path.exists():
        logger.info("config.json not found, so creating default")
        config_path.mkdir(exist_ok=True, parents=True)
        default_config_file_path = resources.files("qbitquick") / "resources" / "default_config.json"
        with default_config_file_path.open("rb") as src, open(config_file_path, "wb") as dst:
            dst.write(src.read())

        logger.info("Created default config.json at: %s", config_file_path)

    with open(config_file_path, "r") as f:
        logger.info("Loading config.json from: %s", config_file_path)
        try:
            config = json.loads(f.read())
        except json.decoder.JSONDecodeError as e:
            raise ValueError(f"Failed to load config.json: {e}") from e
        logger.debug("Loaded config: %s", config)

    try:
        jsonschema.validate(instance=config, schema=CONFIG_SCHEMA, format_checker=format_checker)
    except ValidationError as e:
        raise ValueError(f"Invalid config: {e.message}") from e

    debug_logging = config["debug_logging"] if "debug_logging" in config else False
    _update_log_level(debug_logging)
    logger.info("DEBUG level logging %s", "enabled" if debug_logging else "disabled")
    return config_file_path, config


def _update_log_level(debug_enabled: bool) -> None:
    level = logging.DEBUG if debug_enabled else logging.INFO
    logging.getLogger().setLevel(level)

    for handler in logging.getLogger().handlers:
        handler.setLevel(level)
