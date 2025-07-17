from fastapi import FastAPI

from qbitquick.lifespan import lifespan
from qbitquick.routes import global_exception_handler, router


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    app.add_exception_handler(Exception, global_exception_handler)
    return app