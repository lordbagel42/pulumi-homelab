"""Acknowledged Amp runner controls carried over authenticated phone RPC.

The owner-only control socket and Switchboard bearer authentication protect this
queue. No unauthenticated listener is added to a runner. A plugin claims commands
only for threads it has actually opened. Claimed commands are never redelivered:
an uncertain steering delivery must not become a duplicate user instruction.
"""

import asyncio
import json
import time
import uuid

from amp_client import THREAD_ID


class AmpControl:
    def __init__(self, store, timeout=25):
        self.store = store
        self.timeout = max(1, min(float(timeout), 60))
        self.changed = asyncio.Event()
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS amp_controls (
                id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                action TEXT NOT NULL,
                text TEXT,
                state TEXT NOT NULL DEFAULT 'pending',
                client_id TEXT,
                result TEXT,
                created REAL NOT NULL,
                expires REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS amp_controls_pending
                ON amp_controls(thread_id, state, expires);
        """)

    def _notify(self):
        self.changed.set()
        self.changed = asyncio.Event()

    def _expire(self):
        now = time.time()
        with self.store.db:
            self.store.db.execute(
                "UPDATE amp_controls SET state='expired' WHERE state IN ('pending','claimed') AND expires<=?",
                (now,))
            # Keep only recent control metadata; full instructions remain in the
            # native Amp transcript and the existing phone job history.
            self.store.db.execute("DELETE FROM amp_controls WHERE expires<?", (now - 86400,))

    def _session(self, thread_id):
        if not isinstance(thread_id, str) or not THREAD_ID.fullmatch(thread_id):
            raise ValueError("A native Amp thread ID is required")
        session = self.store.find_thread(thread_id)
        if not session or session.get("engine") != "amp" or session.get("kind") != "managed":
            raise ValueError("This Amp thread is not attached to a managed phone session")
        return session

    async def request(self, action, thread_id, text=None):
        self._session(thread_id)
        if action not in {"cancel", "steer"}:
            raise ValueError("Unsupported Amp control action")
        if action == "steer" and (not isinstance(text, str) or not text.strip() or len(text) > 20000):
            raise ValueError("Provide a steering message of at most 20,000 characters")
        self._expire()
        pending = self.store.db.execute(
            "SELECT COUNT(*) FROM amp_controls WHERE state IN ('pending','claimed')").fetchone()[0]
        if pending >= 100:
            raise RuntimeError("Too many Amp controls are awaiting acknowledgment")
        command_id = str(uuid.uuid4())
        now = time.time()
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO amp_controls(id,thread_id,action,text,created,expires) VALUES(?,?,?,?,?,?)",
                (command_id, thread_id, action, text if action == "steer" else None, now, now + self.timeout))
        self._notify()
        try:
            async with asyncio.timeout(self.timeout):
                while True:
                    wake = self.changed
                    row = self.store.db.execute("SELECT state,result FROM amp_controls WHERE id=?", (command_id,)).fetchone()
                    if row and row["state"] in {"done", "error"}:
                        return json.loads(row["result"])
                    if not row or row["state"] == "expired":
                        break
                    await wake.wait()
        except TimeoutError:
            pass
        finally:
            with self.store.db:
                self.store.db.execute(
                    "UPDATE amp_controls SET state='expired' WHERE id=? AND state IN ('pending','claimed')", (command_id,))
        raise RuntimeError("The Amp runner did not acknowledge the phone control. "
                           "Delivery is unconfirmed; inspect the existing Amp thread before retrying.")

    async def poll(self, thread_ids, client_id, wait_seconds=15, lookup_only=False):
        if (not isinstance(thread_ids, list) or len(thread_ids) > 200
                or any(not isinstance(t, str) or not THREAD_ID.fullmatch(t) for t in thread_ids)):
            raise ValueError("Provide at most 200 native Amp thread IDs")
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= 100:
            raise ValueError("Provide a plugin client ID")
        timeout = max(0, min(float(wait_seconds), 20))
        deadline = time.monotonic() + timeout
        while True:
            wake = self.changed
            self._expire()
            sessions = {}
            for thread_id in thread_ids:
                session = self.store.find_thread(thread_id)
                if session and session.get("engine") == "amp" and session.get("kind") == "managed":
                    sessions[thread_id] = {"id": session["id"], "extension": session["extension"]}
            commands = []
            if sessions and not lookup_only:
                ids = list(sessions)
                marks = ",".join("?" for _ in ids)
                with self.store.db:
                    rows = self.store.db.execute(
                        f"SELECT * FROM amp_controls WHERE thread_id IN ({marks}) AND state='pending' "
                        "AND expires>? ORDER BY created LIMIT 20", (*ids, time.time())).fetchall()
                    for row in rows:
                        changed = self.store.db.execute(
                            "UPDATE amp_controls SET state='claimed',client_id=? WHERE id=? AND state='pending'",
                            (client_id, row["id"])).rowcount
                        if changed:
                            commands.append({key: row[key] for key in ("id", "thread_id", "action", "text", "expires")})
            remaining = deadline - time.monotonic()
            if commands or lookup_only or remaining <= 0 or not thread_ids:
                return {"commands": commands, "sessions": sessions}
            try:
                await asyncio.wait_for(wake.wait(), remaining)
            except asyncio.TimeoutError:
                return {"commands": [], "sessions": sessions}

    def ack(self, command_id, client_id, result):
        if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
            raise ValueError("Provide a valid Amp control acknowledgment")
        self._expire()
        row = self.store.db.execute("SELECT * FROM amp_controls WHERE id=?", (command_id,)).fetchone()
        if not row or row["client_id"] != client_id:
            raise ValueError("Unknown Amp control claim")
        if row["state"] in {"done", "error"}:
            return {"accepted": True, "state": row["state"]}
        if row["state"] != "claimed":
            return {"accepted": False, "state": row["state"]}
        clean = {"ok": result["ok"]}
        if result.get("state") in {"idle", "running", "awaiting-approval", "error"}:
            clean["state"] = result["state"]
        if row["action"] == "cancel" and clean.get("state") not in {"idle", "error"}:
            clean["ok"] = False
        with self.store.db:
            self.store.db.execute("UPDATE amp_controls SET state=?,result=? WHERE id=?",
                                  ("done" if clean["ok"] else "error", json.dumps(clean), command_id))
        self._notify()
        return {"accepted": True, "state": "done" if clean["ok"] else "error"}
