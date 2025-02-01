import os
from logging.handlers import TimedRotatingFileHandler

class SafeTimedRotatingFileHandler(TimedRotatingFileHandler):
    def __init__(self, filename, *args, **kwargs):
        log_dir = os.path.dirname(filename)  # Get directory part of the filename
        if log_dir:  # Only create directories if there is a directory component
            os.makedirs(log_dir, exist_ok=True)
        super().__init__(filename, *args, **kwargs)