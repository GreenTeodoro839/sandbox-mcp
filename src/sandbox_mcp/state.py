"""Tiny SQLite index for sandbox metadata and background jobs (survives restart)."""

import sqlite3
import threading
import time

from .config import config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _c() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        config.STATE_DB.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(config.STATE_DB), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS sandboxes ("
            "name TEXT PRIMARY KEY, image TEXT, created_at REAL, last_used REAL)"
        )
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS jobs ("
            "id TEXT PRIMARY KEY, sandbox TEXT, command TEXT, status TEXT, "
            "exit_code INTEGER, created_at REAL, finished_at REAL)"
        )
        _conn.commit()
    return _conn


def add_sandbox(name: str, image: str) -> None:
    now = time.time()
    with _lock:
        _c().execute(
            "INSERT INTO sandboxes(name, image, created_at, last_used) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET image=excluded.image, last_used=excluded.last_used",
            (name, image, now, now),
        )
        _c().commit()


def touch_sandbox(name: str) -> None:
    with _lock:
        _c().execute(
            "UPDATE sandboxes SET last_used=? WHERE name=?", (time.time(), name)
        )
        _c().commit()


def get_sandbox(name: str) -> dict | None:
    with _lock:
        r = _c().execute("SELECT * FROM sandboxes WHERE name=?", (name,)).fetchone()
    return dict(r) if r else None


def remove_sandbox(name: str) -> None:
    with _lock:
        _c().execute("DELETE FROM sandboxes WHERE name=?", (name,))
        _c().commit()


def add_job(job_id: str, sandbox: str, command: str) -> None:
    with _lock:
        _c().execute(
            "INSERT OR REPLACE INTO jobs"
            "(id, sandbox, command, status, exit_code, created_at, finished_at) "
            "VALUES(?,?,?, 'running', NULL, ?, NULL)",
            (job_id, sandbox, command, time.time()),
        )
        _c().commit()


def get_job(job_id: str) -> dict | None:
    with _lock:
        r = _c().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(r) if r else None


def finish_job(job_id: str, exit_code: int | None) -> None:
    with _lock:
        _c().execute(
            "UPDATE jobs SET status='finished', exit_code=?, finished_at=? "
            "WHERE id=? AND status!='finished'",
            (exit_code, time.time(), job_id),
        )
        _c().commit()
