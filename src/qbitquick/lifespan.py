import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from qbitquick.routes import task_manager

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> Any:
    yield
    task_manager.cancel_all_tasks()
    task_manager.join_all_threads()
