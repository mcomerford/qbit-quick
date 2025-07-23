import logging
import os
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from sqlite3 import Connection, Cursor
from typing import Generator

from platformdirs import user_state_dir
from tabulate import tabulate

from qbitquick.config import APP_NAME, DATABASE_FILENAME, QBQ_STATE_DIR

default_db_dir = user_state_dir(APP_NAME, appauthor=False)
db_path = Path(os.getenv(QBQ_STATE_DIR, default_db_dir))
db_file_path = db_path / f"{DATABASE_FILENAME}"

logger = logging.getLogger(__name__)


def execute_pause_events_ddl(cur: Cursor) -> None:
    ddl_file = resources.files("qbitquick") / "resources" / "pause_events.ddl"
    with ddl_file.open("r") as f:
        cur.executescript(f.read())


@contextmanager
def get_db_connection() -> Generator[tuple[Connection, Cursor], None, None]:
    """
    Returns a database connection and cursor with foreign key constraints enabled.
    """
    conn = sqlite3.connect(db_file_path)
    cur = conn.cursor()

    # Enable foreign key constraints
    cur.execute("PRAGMA foreign_keys = ON")

    execute_pause_events_ddl(cur)

    try:
        yield conn, cur
    finally:
        cur.close()
        conn.close()


def save_torrent_hashes_to_pause(event_id: str, torrent_hashes_to_pause: set[str]) -> None:
    with get_db_connection() as (conn, cur):
        try:
            cur.execute("BEGIN TRANSACTION")

            # Insert the racing torrent hash if it doesn't exist.
            # If it does exist, replace it via a deletion, which cascades to delete the associated paused torrents,
            # followed by an insertion.
            cur.execute("""
                INSERT OR REPLACE INTO pause_events (id)
                VALUES (?)
            """, (event_id,))

            # Insert all the hashes of the paused torrents associated with the given racing torrent
            cur.executemany("""
                INSERT INTO paused_torrents (id, torrent_hash)
                VALUES (?, ?)
            """, [(event_id, torrent_hash_to_pause) for torrent_hash_to_pause in torrent_hashes_to_pause])

            conn.commit()
        except sqlite3.DatabaseError:
            conn.rollback()
            raise


def load_all_paused_torrent_hashes() -> list[str]:
    with get_db_connection() as (conn, cur):
        cur.execute("""
            SELECT DISTINCT torrent_hash
            FROM paused_torrents
        """)

        return [row[0] for row in cur.fetchall()]


def load_torrents_to_unpause(event_id: str) -> list[str]:
    """
    This query gets all the paused torrents associated with the given racing torrent, but excludes any
    torrents that are also paused by other racing torrents, as it implies those haven't finished yet.
    :param event_id: the torrent hash to load the associated paused torrents from
    :return: the list of paused torrent hashes associated with the given torrent hash
    """
    with get_db_connection() as (conn, cur):
        cur.execute("""
            SELECT torrent_hash
            FROM paused_torrents
            GROUP BY torrent_hash
            HAVING COUNT(*) = SUM(id = ?);
        """, (event_id,))

        return [row[0] for row in cur.fetchall()]


def delete_pause_event(event_id: str) -> int:
    with get_db_connection() as (conn, cur):
        cur.execute("""
            DELETE FROM pause_events
            WHERE id = ?
        """, (event_id,))
        conn.commit()

        return cur.rowcount


def print_db() -> int:
    logger.info("Database path: %s", db_file_path)
    headers, table_data = get_table_data()
    print(tabulate(table_data, headers=headers, tablefmt="grid"))
    return 0


def clear_db() -> int:
    with get_db_connection() as (conn, cur):
        # noinspection SqlWithoutWhere
        cur.execute("DELETE FROM pause_events")
        conn.commit()
        logger.info("Database cleared of all [%d] rows", cur.rowcount)
        return 0


def get_table_data() -> tuple[list[str], list[list[str]]]:
    with get_db_connection() as (conn, cur):
        cur.execute("""
            SELECT pe.id,
                   pt.torrent_hash
            FROM pause_events pe
            LEFT JOIN paused_torrents pt
              ON pe.id = pt.id
            ORDER BY pe.created_at, pt.torrent_hash
        """)
        rows = cur.fetchall()

    grouped = defaultdict(list)
    for event_id, torrent_hash in rows:
        if torrent_hash:
            grouped[event_id].append(torrent_hash)
        else:
            grouped.setdefault(event_id, [])

    headers = ["pause_event_id", "paused_torrent_hashes"]
    table_data = [
        [event_id, os.linesep.join(paused_hashes)]
        for event_id, paused_hashes in grouped.items()
    ]
    return headers, table_data


def _render_html_table(headers: list[str], table_data: list[str]) -> str:
    if table_data:
        return tabulate(table_data, headers=headers, tablefmt="html")
    else:
        header_html = "".join(f"<th>{header}</th>" for header in headers)
        return f"""
                <table>
                    <thead><tr>{header_html}</tr></thead>
                </table>
                """
