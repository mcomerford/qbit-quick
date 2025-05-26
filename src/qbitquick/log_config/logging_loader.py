import json
import logging.config
import os
from importlib import resources


def load_logging_config():
    logging_config_path = os.getenv('LOGGING_CONFIG', resources.files('qbitquick') / 'resources' / 'logging_config.json')
    with open(logging_config_path, 'r') as f:
        dict_config = json.load(f)
    logging.config.dictConfig(dict_config)