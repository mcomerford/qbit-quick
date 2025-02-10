import json
import logging.config
import os
from importlib import resources


def load_logging_config():
    script_dir = os.path.dirname(os.path.realpath(__file__))
    #logging_config_path = os.getenv('LOGGING_CONFIG', os.path.join(script_dir, 'logging_config.json'))
    logging_config_path = os.getenv('LOGGING_CONFIG', resources.files('qbitquick') / 'resources' / 'logging_config.json')
    with open(logging_config_path, 'r') as f:
        dict_config = json.load(f)
    logging.config.dictConfig(dict_config)