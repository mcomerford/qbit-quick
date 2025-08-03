import json
import logging
from typing import Any

import jsonschema
from fastapi import HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.routing import APIRoute, APIRouter
from fastapi.templating import Jinja2Templates
from jsonschema import ValidationError
from qbittorrentapi.torrents import TorrentStatusesT
from starlette.responses import Response
from starlette.status import HTTP_200_OK, HTTP_400_BAD_REQUEST, HTTP_500_INTERNAL_SERVER_ERROR

from qbitquick.config import CONFIG_SCHEMA, load_config
from qbitquick.database.database_handler import clear_db, delete_pause_event, get_table_data
from qbitquick.formatters import OutputFormat, format_torrent_info
from qbitquick.handlers import get_torrents_info, pause, post_race, race, unpause
from qbitquick.task_manager import TaskManager
from qbitquick.utils import flatten_fields

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


# noinspection PyShadowingBuiltins,PyUnreachableCode
@router.get("/info")
async def get_torrents_info_route(status: TorrentStatusesT = "all", fields: list[str] | None = Query(default=None), include_field_names: bool = Query(default=False),
                                  format: OutputFormat = Query(default=OutputFormat.json)) -> Response:
    _, config = load_config()
    flattened_fields = flatten_fields(fields)
    filtered = get_torrents_info(config, status, flattened_fields)
    formatted_output = format_torrent_info(filtered, include_field_names, format)

    match format:
        case OutputFormat.json:
            return Response(content=formatted_output, status_code=HTTP_200_OK, media_type="application/json")
        case OutputFormat.plain:
            return Response(content=formatted_output, status_code=HTTP_200_OK, media_type="text/plain")
        case _:
            return JSONResponse(
                status_code=HTTP_500_INTERNAL_SERVER_ERROR,
                content={
                    "status": "error",
                    "reason": f"unsupported format: {format}"
                })


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
        json.dump(data, f, indent=2)

    return {
        "status": "success",
        "message": f"config successfully saved to: {config_file_path}",
    }


@router.get("/db", response_class=HTMLResponse)
async def get_db_route(request: Request) -> HTMLResponse:
    headers, rows = get_table_data()
    return templates.TemplateResponse(request=request, name="db_view.html", context={
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
