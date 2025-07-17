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


@contextmanager
def get_db_connection() -> Generator[tuple[Connection, Cursor], None, None]:
    """
    Returns a database connection and cursor with foreign key constraints enabled.
    """
    conn = sqlite3.connect(db_file_path)
    cur = conn.cursor()

    # Enable foreign key constraints
    cur.execute("PRAGMA foreign_keys = ON")

    try:
        yield conn, cur
    finally:
        cur.close()
        conn.close()


def save_torrent_hashes_to_pause(racing_torrent_hash: str, torrent_hashes_to_pause: set[str]) -> None:
    with get_db_connection() as (conn, cur):
        ddl_file = resources.files("qbitquick") / "resources" / "race.ddl"
        with ddl_file.open("r") as f:
            cur.executescript(f.read())
        try:
            cur.execute("BEGIN TRANSACTION")

            # Insert the racing torrent hash if it doesn't exist.
            # If it does exist, replace it via a deletion, which cascades to delete the associated paused torrents,
            # followed by an insertion.
            cur.execute("""
                INSERT OR REPLACE INTO racing_torrents (racing_torrent_hash)
                VALUES (?)
            """, (racing_torrent_hash,))

            # Insert all the hashes of the paused torrents associated with the given racing torrent
            cur.executemany("""
                INSERT INTO paused_torrents (racing_torrent_hash, paused_torrent_hash)
                VALUES (?, ?)
            """, [(racing_torrent_hash, torrent_hash_to_pause) for torrent_hash_to_pause in torrent_hashes_to_pause])

            conn.commit()
        except sqlite3.DatabaseError:
            conn.rollback()
            raise


def load_all_paused_torrent_hashes() -> list[str]:
    with get_db_connection() as (conn, cur):
        cur.execute("""
            SELECT DISTINCT paused_torrent_hash
            FROM paused_torrents
        """)

        return [row[0] for row in cur.fetchall()]


def load_torrents_to_unpause(torrent_hash: str) -> list[str]:
    """
    This query gets all the paused torrents associated with the given racing torrent, but excludes any
    torrents that are also paused by other racing torrents, as it implies those haven't finished yet.
    :param torrent_hash: the torrent hash to load the associated paused torrents from
    :return: the list of paused torrent hashes associated with the given torrent hash
    """
    with get_db_connection() as (conn, cur):
        cur.execute("""
            SELECT paused_torrent_hash
            FROM paused_torrents
            GROUP BY paused_torrent_hash
            HAVING COUNT(*) = SUM(racing_torrent_hash = ?);
        """, (torrent_hash,))

        return [row[0] for row in cur.fetchall()]


def delete_torrent(torrent_hash: str) -> int:
    with get_db_connection() as (conn, cur):
        cur.execute("""
            DELETE
            FROM racing_torrents
            WHERE racing_torrent_hash = ?
        """, (torrent_hash,))
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
        cur.execute("DELETE FROM racing_torrents")
        conn.commit()
        logger.info("Database cleared of all [%d] rows", cur.rowcount)
        return 0


def get_table_data() -> tuple[list[str], list[list[str]]]:
    with get_db_connection() as (conn, cur):
        cur.execute("""
            SELECT rt.racing_torrent_hash,
                   pt.paused_torrent_hash
            FROM racing_torrents rt
            LEFT JOIN paused_torrents pt
              ON rt.racing_torrent_hash = pt.racing_torrent_hash
            ORDER BY rt.racing_torrent_hash, pt.paused_torrent_hash
        """)
        rows = cur.fetchall()

    grouped: dict[str, list[str]] = defaultdict(list)
    for racing_torrent_hash, paused_torrent_hash in rows:
        if paused_torrent_hash:
            grouped[racing_torrent_hash].append(paused_torrent_hash)
        else:
            grouped.setdefault(racing_torrent_hash, [])

    headers = ["racing_torrent_hash", "paused_torrent_hashes"]
    table_data = [
        [racing_hash, os.linesep.join(paused_hashes)]
        for racing_hash, paused_hashes in grouped.items()
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
