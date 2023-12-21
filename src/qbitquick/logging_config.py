import os

LOGGING_CONFIG = {
    'version': 1,
    'formatters': {
        'default': {
            'format': '%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s',
            'datefmt': '%Y-%m-%d %H:%M:%S'
        }
    },
    'handlers': {
        'console': {
            'level': 'DEBUG',
            'class': 'logging.StreamHandler',
            'formatter': 'default',
            'stream': 'ext://sys.stdout'
        },
        'file': {
            'level': 'DEBUG',
            'class': 'logging.handlers.TimedRotatingFileHandler',
            'when': 'midnight',
            'backupCount': 7,
            'formatter': 'default',
            'filename': 'qbit_quick.log',
        }
    },
    'loggers': {
        'root': {
            'level': os.getenv('LOG_LEVEL', 'DEBUG'),
            'handlers': ['console', 'file']
        }
    },
    'disable_existing_loggers': False
}
