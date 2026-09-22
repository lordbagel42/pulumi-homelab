"""Durable, at-most-once call attempts, including across process restarts."""

import sqlite3
import math
import re
from pathlib import Path

from .huddles import Huddle


class State:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS huddles (
                key TEXT PRIMARY KEY,
                started_at REAL NOT NULL,
                seen_at REAL NOT NULL,
                status TEXT NOT NULL,
                ended INTEGER NOT NULL DEFAULT 0
            )
        """)
        self.db.commit()
        self.db.execute("""CREATE TABLE IF NOT EXISTS outgoing (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, mode TEXT NOT NULL,
            created_at REAL NOT NULL, status TEXT NOT NULL)""")
        self.db.execute("UPDATE outgoing SET status='interrupted' WHERE status='attempted'")
        self.db.execute("""CREATE TABLE IF NOT EXISTS recent_contacts (
            user_id TEXT PRIMARY KEY, connected_at REAL NOT NULL)""")
        self.db.commit()

    def record_contact(self, user_id, connected_at):
        if not isinstance(user_id,str) or not re.fullmatch(r"[UW][A-Z0-9]+",user_id):
            raise ValueError("Invalid Slack member ID")
        if not isinstance(connected_at,(int,float)) or not math.isfinite(connected_at) or connected_at <= 0:
            raise ValueError("Invalid huddle connection timestamp")
        with self.db:
            self.db.execute("""INSERT INTO recent_contacts VALUES(?,?)
                ON CONFLICT(user_id) DO UPDATE SET connected_at=excluded.connected_at
                WHERE excluded.connected_at > recent_contacts.connected_at""",(user_id,connected_at))

    def recent_contacts(self):
        return [{"user_id":row[0],"connected_at":row[1]} for row in self.db.execute(
            "SELECT user_id,connected_at FROM recent_contacts ORDER BY connected_at DESC LIMIT 100")]

    def outgoing(self, request_id):
        row = self.db.execute("SELECT user_id, mode, status FROM outgoing WHERE id=?", (request_id,)).fetchone()
        return dict(zip(("user_id", "mode", "status"), row)) if row else None

    def claim_outgoing(self, request_id, user_id, mode, now):
        with self.db:
            result = self.db.execute("INSERT OR IGNORE INTO outgoing VALUES (?, ?, ?, ?, 'attempted')",
                                     (request_id, user_id, mode, now))
            return result.rowcount == 1

    def finish_outgoing(self, request_id, status):
        with self.db:
            self.db.execute("UPDATE outgoing SET status=? WHERE id=?", (status, request_id))

    def observe(self, huddle: Huddle, now: float) -> None:
        with self.db:
            self.db.execute("""
                INSERT INTO huddles(key, started_at, seen_at, status, ended)
                VALUES (?, ?, ?, 'pending', ?)
                ON CONFLICT(key) DO UPDATE SET
                    seen_at = excluded.seen_at,
                    ended = MAX(huddles.ended, excluded.ended)
            """, (huddle.key, huddle.started_at, now, int(huddle.ended)))

    def claim(self, huddle: Huddle, now: float, max_age: float) -> bool:
        """Commit before external side effects. An ambiguous failed call is
        deliberately not retried: a timeout could mean the phone already rang.
        """
        self.observe(huddle, now)
        with self.db:
            if not -5 <= now - huddle.started_at <= max_age:
                self.db.execute("UPDATE huddles SET status='stale' WHERE key=? AND status='pending'",
                                (huddle.key,))
                return False
            result = self.db.execute("""
                UPDATE huddles SET status='attempted'
                WHERE key=? AND status='pending' AND ended=0
            """, (huddle.key,))
            return result.rowcount == 1

    def finish(self, huddle: Huddle, status: str) -> None:
        with self.db:
            self.db.execute("UPDATE huddles SET status=? WHERE key=?", (status, huddle.key))

    def is_ended(self, huddle: Huddle) -> bool:
        row = self.db.execute("SELECT ended FROM huddles WHERE key=?", (huddle.key,)).fetchone()
        return bool(row and row[0])

    def prune(self, now: float) -> None:
        with self.db:
            self.db.execute("DELETE FROM huddles WHERE seen_at < ?", (now - 7 * 86400,))
            self.db.execute("DELETE FROM outgoing WHERE created_at < ?", (now - 7 * 86400,))

    def close(self) -> None:
        self.db.close()
