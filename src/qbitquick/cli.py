import json
import logging
import signal
import sys
import threading
import time
from typing import Annotated, cast, get_args

import click
import typer
from qbittorrentapi.torrents import TorrentStatusesT
from tabulate import tabulate  # type: ignore

from qbitquick.config import APP_NAME, load_config, update_log_level
from qbitquick.database.database_handler import clear_db, db_file_path, delete_pause_event, get_table_data
from qbitquick.formatters import OutputFormat, format_torrent_info
from qbitquick.handlers import edit_config, get_torrents_info, pause, post_race, race, start_server, unpause
from qbitquick.server import create_app
from qbitquick.utils import flatten_fields

logger = logging.getLogger(__name__)
app = typer.Typer(name=APP_NAME, no_args_is_help=True)
stop_event = threading.Event()

VERBOSITY_MAP = {
    0: logging.ERROR,  # default if no -v
    1: logging.INFO,   # -v
    2: logging.DEBUG,  # -vv
}


def _setup_cli_shutdown_hook() -> None:
    def handle_sigint(_sig, _frame):
        typer.echo("Interrupted via Ctrl+C, signalling shutdown...")
        stop_event.set()
        time.sleep(1)
        sys.exit(1)

    signal.signal(signal.SIGINT, handle_sigint)


@app.callback()
def main(
    verbose: int = typer.Option(
        0,
        "--verbose",
         "-v",
        count=True,
        help="Increase verbosity (-v, -vv).",
    ),
):
    console_level = VERBOSITY_MAP.get(verbose, logging.DEBUG) ## Default to debug if more than -vv is used
    update_log_level("console", console_level)


@app.command("server", help="Run in server mode")
def server(port: Annotated[int, typer.Option(help="Server port")] = 8081):
    app_instance = create_app()
    start_server(app_instance, port)


@app.command("race", help="Race the provided torrent")
def race_cmd(torrent_hash: Annotated[str, typer.Argument(help="Hash of the torrent to race")]):
    _, config = load_config()
    _setup_cli_shutdown_hook()
    raise typer.Exit(code=race(config, torrent_hash, stop_event))


@app.command("post-race", help="Run the post race steps for the provided torrent, such as resuming torrents that were previously paused")
def post_race_cmd(torrent_hash: Annotated[str, typer.Argument(help="Hash of the completed torrent")]):
    _, config = load_config()
    raise typer.Exit(code=post_race(config, torrent_hash))


# noinspection PyShadowingBuiltins
@app.command("pause", help="Pause any torrents that match the criteria specified in the config")
def pause_cmd(id: Annotated[str, typer.Option(help="A unique identifier for this pause event, which is needed to call unpause")] = "pause"):
    _, config = load_config()
    raise typer.Exit(code=pause(config, id))


# noinspection PyShadowingBuiltins
@app.command("unpause", help="Unpauses any torrents that were paused using the pause command")
def unpause_cmd(id: Annotated[str, typer.Option(help="The identifier that was used when pausing the torrents")] = "pause"):
    _, config = load_config()
    raise typer.Exit(code=unpause(config, id))


# noinspection PyShadowingBuiltins
@app.command("info", help="Retrieve torrent information. For the full list of field names see:\n"
                                "https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-5.0)#torrent-management")
def info_cmd(
        status: Annotated[
            str,
            typer.Option(
                click_type=click.Choice(get_args(TorrentStatusesT)),
                help="Torrent status filter",
            )] = "all",
        fields: Annotated[
            list[str] | None,
            typer.Option(
                "--fields",
                "-f",
                help="Fields to include. Accepts '-f name -f size' or '--fields name,size'",
                show_default="all",
            )] = None,
        include_field_names: Annotated[
            bool,
            typer.Option(
                "--include-field-names",  # Needed so that typer doesn't auto-generate a --no-include-field-names flag as well
                help="Include field names in output",
            )] = False,
        format: Annotated[
            OutputFormat,
            typer.Option(
                help="Output format"
            )] = OutputFormat.plain,
):
    _, config = load_config()
    typed_status = cast(TorrentStatusesT, status)
    flattened_fields = flatten_fields(fields)
    torrents_info = get_torrents_info(config, typed_status, flattened_fields)
    formatted_torrent_info = format_torrent_info(torrents_info, include_field_names, format)
    typer.echo(formatted_torrent_info)


# noinspection PyShadowingBuiltins
@app.command("config", help="Print or edit the current config")
def config_cmd(
        print: Annotated[bool, typer.Option("--print", help="Print the current config")] = False,
        edit: Annotated[bool, typer.Option("--edit", help="Edit the config file")] = False,
):
    config_path, config = load_config()
    if print:
        typer.echo(f"Config Path: {config_path}")
        typer.echo(json.dumps(config, indent=2))
    elif edit:
        raise typer.Exit(code=edit_config(str(config_path)))
    else:
        typer.echo("You must pass one of --print or --edit")
        raise typer.Exit(code=1)


# noinspection PyShadowingBuiltins
@app.command("db", help="Perform actions on the SQLite database")
def db_cmd(
        print: Annotated[bool, typer.Option("--print", help="Print the contents of the database")] = False,
        clear: Annotated[bool, typer.Option("--clear", help="Clear the database")] = False,
        delete: Annotated[str | None, typer.Option(help="Delete the specified entry from the database")] = None,
):
    if print:
        logger.info("Database path: %s", db_file_path)
        headers, table_data = get_table_data()
        typer.echo(tabulate(table_data, headers=headers, tablefmt="grid"))
    elif clear:
        confirm = input("This will delete ALL entries from the database. Are you sure? (y/n): ")
        if confirm.lower() == "y":
            raise typer.Exit(code=clear_db())
    elif delete:
        deleted_rows = delete_pause_event(delete)
        typer.echo(f"{deleted_rows} row deleted" if deleted_rows == 1 else f"{deleted_rows} rows deleted")
    else:
        typer.echo("You must pass one of --print, --clear, or --delete")
        raise typer.Exit(code=1)
