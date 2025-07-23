import logging
from typing import Any

import jsonschema
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.routing import APIRoute, APIRouter
from fastapi.templating import Jinja2Templates
from jsonschema import ValidationError
from starlette.status import HTTP_400_BAD_REQUEST, HTTP_500_INTERNAL_SERVER_ERROR

from qbitquick.config import CONFIG_SCHEMA, load_config
from qbitquick.database.database_handler import clear_db, delete_pause_event, get_table_data
from qbitquick.handlers import pause, post_race, race, unpause
from qbitquick.task_manager import TaskManager

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")
task_manager = TaskManager()


@router.post("/race/{torrent_hash}")
async def race_route(torrent_hash: str) -> dict[str, str]:
    _, config = load_config()
    task_id = task_manager.start_task(race, config, torrent_hash, task_name=f"race {torrent_hash}")
    return {
        "status": "accepted",
        "task_id": task_id
    }


@router.post("/post-race/{torrent_hash}")
async def post_race_route(torrent_hash: str) -> dict[str, str]:
    _, config = load_config()
    post_race(config, torrent_hash)
    return {
        "status": "success",
        "message": "post race ran successfully",
    }


@router.post("/pause")
@router.post("/pause/{event_id}")
async def pause_route(event_id: str = "pause") -> dict[str, str]:
    _, config = load_config()
    pause(config, event_id)
    return {
        "status": "success",
        "message": "torrents paused successfully",
    }


@router.post("/unpause")
@router.post("/unpause/{event_id}")
async def unpause_route(event_id: str = "pause") -> dict[str, str]:
    _, config = load_config()
    unpause(config, event_id)
    return {
        "status": "success",
        "message": "torrents unpaused successfully",
    }


@router.api_route("/cancel/{task_id}", methods=["DELETE", "POST"])
async def cancel_task_route(task_id: str) -> dict[str, str]:
    logger.info("Requesting to cancel task [%s]", task_id)
    if task_manager.cancel_task(task_id):
        return {
            "status": "success",
            "message": "task successfully cancelled"
        }
    else:
        logger.error("No task found with id [%s], so nothing to cancel", task_id)
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=f"No task found with id {task_id}")


@router.get("/tasks")
async def get_running_tasks_route() -> dict[str, Any]:
    return task_manager.get_running_tasks()


@router.get("/config")
async def get_config_route() -> dict[str, Any]:
    _, config = load_config()
    return config


@router.api_route("/config", methods=["POST", "PUT"])
async def save_config_route(request: Request) -> dict[str, str]:
    try:
        data = await request.json()
        jsonschema.validate(instance=data, schema=CONFIG_SCHEMA)
    except ValidationError as e:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(e))

    config_file_path, _ = load_config()
    with config_file_path.open("w") as f:
        import json
        json.dump(data, f, indent=2)

    return {
        "status": "success",
        "message": f"config successfully saved to: {config_file_path}",
    }


@router.get("/db", response_class=HTMLResponse)
async def get_db_route(request: Request) -> HTMLResponse:
    headers, rows = get_table_data()
    return templates.TemplateResponse("db_view.html", {
        "request": request,
        "headers": headers,
        "rows": rows,
    })


@router.delete("/db")
@router.delete("/db/{torrent_hash}")
async def delete_db_route(torrent_hash: str | None = None) -> dict[str, str]:
    if torrent_hash:
        delete_pause_event(torrent_hash)
        return {
            "status": "success",
            "message": f"{torrent_hash} deleted from database"
        }
    else:
        clear_db()
        return {
            "status": "success",
            "message": "database cleared"
        }


@router.get("/")
async def list_routes() -> list[dict[str, Any]]:
    return [
        {
            "method": sorted(route.methods),
            "path": route.path
        }
        for route in router.routes if isinstance(route, APIRoute)
    ]


async def global_exception_handler(_request: Request, exception: Exception) -> JSONResponse:
    logger.exception("Unhandled exception")
    return JSONResponse(
        status_code=HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "status": "error",
            "reason": str(exception)
        },
    )
