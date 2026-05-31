"""SQLite persistence for chat sessions (survives restarts on cloud hosts)."""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "bot_data.sqlite3")


class Storage:
    def __init__(self, path: str = DB_PATH) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    chat_id INTEGER PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    agent_url TEXT,
                    repo_url TEXT,
                    branch TEXT DEFAULT 'main',
                    updated_at REAL DEFAULT (strftime('%s','now'))
                );
                CREATE TABLE IF NOT EXISTS usage (
                    user_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    runs INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, day)
                );
                CREATE TABLE IF NOT EXISTS chat_prefs (
                    chat_id INTEGER PRIMARY KEY,
                    repo_url TEXT,
                    branch TEXT DEFAULT 'main'
                );
                """
            )

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get_session(self, chat_id: int) -> dict | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            return dict(row) if row else None

    def set_session(
        self,
        chat_id: int,
        agent_id: str,
        agent_url: str | None = None,
        repo_url: str | None = None,
        branch: str | None = None,
    ) -> None:
        prefs = self.get_repo_prefs(chat_id)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (chat_id, agent_id, agent_url, repo_url, branch)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    agent_id = excluded.agent_id,
                    agent_url = COALESCE(excluded.agent_url, sessions.agent_url),
                    repo_url = COALESCE(excluded.repo_url, sessions.repo_url),
                    branch = COALESCE(excluded.branch, sessions.branch),
                    updated_at = strftime('%s','now')
                """,
                (
                    chat_id,
                    agent_id,
                    agent_url,
                    repo_url or prefs[0],
                    branch or prefs[1],
                ),
            )

    def clear_session(self, chat_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE chat_id = ?", (chat_id,))

    def set_repo_prefs(self, chat_id: int, repo_url: str, branch: str = "main") -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO chat_prefs (chat_id, repo_url, branch)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    repo_url = excluded.repo_url,
                    branch = excluded.branch
                """,
                (chat_id, repo_url, branch),
            )
            conn.execute(
                """
                UPDATE sessions SET repo_url = ?, branch = ?
                WHERE chat_id = ?
                """,
                (repo_url, branch, chat_id),
            )

    def get_repo_prefs(self, chat_id: int) -> tuple[str | None, str]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT repo_url, branch FROM chat_prefs WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            if row and row["repo_url"]:
                return row["repo_url"], row["branch"] or "main"
            row = conn.execute(
                "SELECT repo_url, branch FROM sessions WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            if row and row["repo_url"]:
                return row["repo_url"], row["branch"] or "main"
        return None, "main"

    def increment_daily_runs(self, user_id: int, day: str) -> int:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO usage (user_id, day, runs) VALUES (?, ?, 1)
                ON CONFLICT(user_id, day) DO UPDATE SET runs = runs + 1
                """,
                (user_id, day),
            )
            row = conn.execute(
                "SELECT runs FROM usage WHERE user_id = ? AND day = ?",
                (user_id, day),
            ).fetchone()
            return int(row["runs"]) if row else 1

    def get_daily_runs(self, user_id: int, day: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT runs FROM usage WHERE user_id = ? AND day = ?",
                (user_id, day),
            ).fetchone()
            return int(row["runs"]) if row else 0
