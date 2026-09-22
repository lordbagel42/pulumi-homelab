"""Durable phone extensions, work queue, and caller-provided answers."""

import json
import re
import sqlite3
import time
import uuid
from pathlib import Path


def extension(number):
    return f"611{number:03d}"


def session_number(value):
    value = str(value).replace("-", "")
    if len(value) == 6 and value.startswith("611"):
        value = value[3:]
    if not value.isdecimal() or not 1 <= int(value) <= 999:
        raise ValueError("Use an existing session number from 001 to 999")
    return int(value)


def call_uuid(number=0):
    if not 0 <= number <= 999:
        raise ValueError("Invalid phone session")
    return uuid.UUID(hex=f"{number:03d}" + uuid.uuid4().hex[3:])


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(id <= 999),
                kind TEXT NOT NULL, external_key TEXT UNIQUE, thread_id TEXT UNIQUE,
                title TEXT NOT NULL, cwd TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'idle',
                last_reply TEXT NOT NULL DEFAULT '', progress TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES sessions(id),
                text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                reply TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS questions (
                id TEXT PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES sessions(id),
                source TEXT NOT NULL, request_key TEXT UNIQUE,
                questions TEXT NOT NULL, answers TEXT NOT NULL DEFAULT '{}',
                state TEXT NOT NULL DEFAULT 'pending',
                ring_attempted INTEGER NOT NULL DEFAULT 0,
                ring_state TEXT NOT NULL DEFAULT 'queued', call_file TEXT,
                created REAL NOT NULL, updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES sessions(id),
                text TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL
            );
        """)
        # Existing callback numbers already live on Proxmox after migration.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(sessions)")}
        if "host" not in columns:
            with self.db:
                self.db.execute("ALTER TABLE sessions ADD COLUMN host TEXT NOT NULL DEFAULT 'proxmox'")
        with self.db:
            if "engine" not in columns:
                self.db.execute("ALTER TABLE sessions ADD COLUMN engine TEXT NOT NULL DEFAULT 'codex'")
            if "executor" not in columns:
                self.db.execute("ALTER TABLE sessions ADD COLUMN executor TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _session(row):
        if row is None:
            return None
        data = dict(row)
        data["extension"] = extension(data["id"])
        data["display_number"] = "611-" + f'{data["id"]:03d}'
        thread = data.get("thread_id") or ""
        data["thread_url"] = ("https://ampcode.com/threads/" + thread
            if data.get("engine") == "amp" and re.fullmatch(r"T-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", thread)
            else "")
        return data

    def create_session(self, title, cwd, kind="managed", external_key=None, thread_id=None, host="proxmox", engine="codex", executor=""):
        if kind not in ("managed", "external"):
            raise ValueError("Invalid session kind")
        if host not in ("proxmox", "workstation", "choose"):
            raise ValueError("Choose Proxmox or workstation")
        if engine not in {"amp", "codex"}:
            raise ValueError("Choose Amp or Codex")
        if external_key:
            row = self.db.execute("SELECT * FROM sessions WHERE external_key=?", (external_key,)).fetchone()
            if row:
                return self._session(row)
        now = time.time()
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO sessions(kind,external_key,thread_id,title,cwd,host,engine,executor,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (kind, external_key, thread_id, title[:200], cwd, host, engine, executor, now, now))
        return self.get_session(cursor.lastrowid)

    def get_session(self, number):
        row = self.db.execute("SELECT * FROM sessions WHERE id=?", (session_number(number),)).fetchone()
        if row is None:
            raise KeyError("That phone session does not exist")
        return self._session(row)

    def find_thread(self, thread_id):
        return self._session(self.db.execute("SELECT * FROM sessions WHERE thread_id=?", (thread_id,)).fetchone())

    def bind_amp_thread(self, number, previous, native, executor):
        if not re.fullmatch(r"T-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", native or ""):
            raise ValueError("Invalid native Amp thread ID")
        with self.db:
            changed = self.db.execute(
                "UPDATE sessions SET thread_id=?,updated=? WHERE id=? AND engine='amp' AND thread_id=? AND executor=?",
                (native, time.time(), session_number(number), previous, executor)).rowcount
        if not changed:
            raise ValueError("The Amp thread does not match its saved phone session")
        return self.get_session(number)

    def list_sessions(self):
        return [self._session(r) for r in self.db.execute("SELECT * FROM sessions ORDER BY id")]

    def update_session(self, number, **values):
        allowed = {"title", "thread_id", "state", "last_reply", "progress", "error"}
        if not values or not set(values) <= allowed:
            raise ValueError("Invalid session update")
        values["updated"] = time.time()
        with self.db:
            self.db.execute("UPDATE sessions SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?",
                            (*values.values(), session_number(number)))

    def select_host(self, number, host, cwd, executor=""):
        if host not in ("proxmox", "workstation"):
            raise ValueError("Choose Proxmox or workstation")
        with self.db:
            changed = self.db.execute(
                "UPDATE sessions SET host=?,cwd=?,executor=?,updated=? WHERE id=? AND host='choose' AND thread_id IS NULL",
                (host, cwd, executor, time.time(), session_number(number))).rowcount
        if not changed:
            raise ValueError("A saved session stays on its original host; start a new session to choose another")
        return self.get_session(number)

    def add_job(self, number, text):
        now = time.time()
        with self.db:
            cursor = self.db.execute("INSERT INTO jobs(session_id,text,created,updated) VALUES(?,?,?,?)",
                                     (session_number(number), text, now, now))
        return cursor.lastrowid

    def next_job(self, number):
        row = self.db.execute("SELECT * FROM jobs WHERE session_id=? AND state='queued' ORDER BY id LIMIT 1",
                              (number,)).fetchone()
        return dict(row) if row else None

    def last_job(self, number):
        row = self.db.execute("SELECT * FROM jobs WHERE session_id=? ORDER BY id DESC LIMIT 1",
                              (number,)).fetchone()
        return dict(row) if row else None

    def update_job(self, job_id, state, reply=""):
        with self.db:
            self.db.execute("UPDATE jobs SET state=?,reply=?,updated=? WHERE id=?",
                            (state, reply, time.time(), job_id))

    def recover(self):
        """Keep the transcript; never silently replay potentially completed side effects."""
        with self.db:
            self.db.execute("UPDATE jobs SET state='interrupted' WHERE state='running'")
            self.db.execute("""UPDATE sessions SET state='interrupted',
                progress='The bridge restarted during the task. Tell me to continue when ready.'
                WHERE state IN ('running','waiting') AND kind='managed' AND engine='codex'""")
            self.db.execute("""UPDATE sessions SET state='error',
                error='The phone bridge restarted. Your Amp runner may still be working; inspect the native Amp thread before continuing.',
                progress='The phone connection to Amp was lost; the runner was not cancelled.'
                WHERE state IN ('running','waiting') AND kind='managed' AND engine='amp'""")

    def cancel_pending(self, number):
        with self.db:
            self.db.execute("UPDATE jobs SET state='cancelled' WHERE session_id=? AND state='queued'", (number,))
            self.db.execute("UPDATE questions SET state='cancelled' WHERE session_id=? AND state='pending'", (number,))

    @staticmethod
    def _question(row):
        if row is None:
            return None
        result = dict(row)
        result["questions"] = json.loads(result["questions"])
        result["answers"] = json.loads(result["answers"])
        result["extension"] = extension(result["session_id"])
        return result

    def add_question(self, number, questions, source, request_key=None):
        if request_key:
            row = self.db.execute("SELECT * FROM questions WHERE request_key=?", (request_key,)).fetchone()
            if row:
                return self._question(row)
        if not questions or len(questions) > 8:
            raise ValueError("Provide between one and eight questions")
        ids = set()
        for question in questions:
            if not question.get("id") or not question.get("question", "").strip():
                raise ValueError("Each question needs an id and text")
            if question["id"] in ids:
                raise ValueError("Question ids must be unique")
            ids.add(question["id"])
            if question.get("isSecret"):
                raise ValueError("Secret input is not collected over the phone")
        now, question_id = time.time(), str(uuid.uuid4())
        with self.db:
            self.db.execute("""INSERT INTO questions(id,session_id,source,request_key,questions,created,updated)
                               VALUES(?,?,?,?,?,?,?)""",
                            (question_id, session_number(number), source, request_key, json.dumps(questions), now, now))
        return self.get_question(question_id)

    def get_question(self, question_id):
        row = self.db.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()
        if row is None:
            raise KeyError("Unknown phone question")
        return self._question(row)

    def pending_questions(self, number=None):
        if number is None:
            rows = self.db.execute("SELECT * FROM questions WHERE state='pending' ORDER BY created")
        else:
            rows = self.db.execute("SELECT * FROM questions WHERE state='pending' AND session_id=? ORDER BY created",
                                   (number,))
        return [self._question(row) for row in rows]

    def record_answer(self, question_id, item_id, answer):
        question = self.get_question(question_id)
        if question["state"] != "pending":
            raise ValueError("That question has already been answered")
        if item_id not in {item["id"] for item in question["questions"]}:
            raise ValueError("Unknown question item")
        if not answer.strip():
            raise ValueError("An empty response is not an answer")
        answers = question["answers"]
        answers[item_id] = {"answers": [answer.strip()]}
        complete = len(answers) == len(question["questions"])
        with self.db:
            self.db.execute("UPDATE questions SET answers=?,state=?,updated=? WHERE id=?",
                            (json.dumps(answers), "answered" if complete else "pending", time.time(), question_id))
        return self.get_question(question_id)

    def mark_ring(self, question_id, state, call_file=None):
        with self.db:
            self.db.execute("UPDATE questions SET ring_attempted=1,ring_state=?,call_file=?,updated=? WHERE id=?",
                            (state, call_file, time.time(), question_id))

    def add_inbox(self, number, text):
        with self.db:
            self.db.execute("INSERT INTO inbox(session_id,text,created) VALUES(?,?,?)", (number, text, time.time()))

    def inbox(self, number, acknowledge=False):
        rows = [dict(row) for row in self.db.execute(
            "SELECT * FROM inbox WHERE session_id=? AND acknowledged=0 ORDER BY id", (number,))]
        if acknowledge and rows:
            with self.db:
                self.db.executemany("UPDATE inbox SET acknowledged=1 WHERE id=?", [(r["id"],) for r in rows])
        return rows
