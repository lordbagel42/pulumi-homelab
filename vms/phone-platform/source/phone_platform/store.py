"""Private SQLite configuration, app registrations and ordered event history."""
import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path


INTEGRATIONS = [
    ("amp", "Amp", "operator", True, {"host": "proxmox"}),
    ("elevenlabs-speech-engine", "ElevenLabs Speech Engine", "speech-engine", False, {"engine_id": "", "upstream_url": "", "voice_id": "", "model_id": "eleven_flash_v2_5", "language": "en"}),
    ("codex", "Codex", "operator", True, {"host": "proxmox"}),
    ("slack-huddles", "Slack huddles", "app", True, {}),
    ("home-assistant", "Home Assistant", "app", True, {}),
    ("whisper", "Whisper", "stt", True, {"model": "base.en", "language": "en"}),
    ("elevenlabs-stt", "ElevenLabs Scribe", "stt", False, {"model_id": "scribe_v2", "language": "en"}),
    ("piper", "Piper", "tts", True, {"voice": "en_US-lessac-medium"}),
    ("elevenlabs-tts", "ElevenLabs Voice", "tts", False, {"model_id": "eleven_multilingual_v2", "voice_id": ""}),
]
ROUTES = [
    {"id": "codex", "name": "Talk to Amp", "extension": "611", "integration": "amp", "stt": "default", "tts": "default", "enabled": True},
    {"id": "home-assistant", "name": "Home Assistant", "extension": "555", "integration": "home-assistant", "stt": "default", "tts": "default", "enabled": True},
    {"id": "slack-operator", "name": "Slack operator", "extension": "0", "integration": "slack-huddles", "stt": "default", "tts": "default", "enabled": True},
]


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        self.directory = directory
        self.db = sqlite3.connect(directory / "switchboard.sqlite3", check_same_thread=False)
        (directory / "switchboard.sqlite3").chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS documents (kind TEXT, id TEXT, data TEXT NOT NULL, PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS credentials (id TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT,type TEXT,session_id TEXT,message TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS logins (digest TEXT PRIMARY KEY,expires REAL,kind TEXT);
            CREATE TABLE IF NOT EXISTS contacts (id INTEGER PRIMARY KEY AUTOINCREMENT,user_id TEXT UNIQUE,name TEXT,username TEXT,updated REAL);
        """)
        if self.get("settings", "main") is None:
            self.put("settings", "main", {"name": "Switchboard", "default_stt": "whisper", "default_tts": "piper", "default_host": "proxmox"})
        for id, name, kind, enabled, config in INTEGRATIONS:
            if self.get("integrations", id) is None:
                self.put("integrations", id, {"id": id, "name": name, "kind": kind, "enabled": enabled, "config": config})
        for route in ROUTES:
            if self.get("routes", route["id"]) is None:
                self.put("routes", route["id"], route)
        # Keep the 611 route ID, aliases and speech/profile choices for adapters.
        main_route = self.get("routes", "codex")
        if main_route["integration"] == "codex":
            main_route["integration"] = "amp"
            if main_route["name"] == "Talk to Codex":
                main_route["name"] = "Talk to Amp"
            self.put("routes", "codex", main_route)
        for id, name, integration in [("amp", "Amp sessions", "amp"), ("codex", "Codex sessions", "codex"), ("slack-recent", "Recent Slack huddles", "slack-huddles")]:
            if self.get("directories", id) is None:
                self.put("directories", id, {"id": id, "name": name, "integration": integration, "enabled": True, "builtin": True, "entries": []})
        self.admin_token = self.token_file("admin-token")
        self.phone_token = self.token_file("phone-token")

    def token_file(self, name):
        path = self.directory / name
        if not path.exists():
            path.write_text(secrets.token_urlsafe(32) + "\n")
        path.chmod(0o600)
        value = path.read_text().strip()
        if len(value) < 32:
            raise ValueError(f"{name} must contain a strong token of at least 32 characters")
        return value

    def get(self, kind, id, default=None):
        row = self.db.execute("SELECT data FROM documents WHERE kind=? AND id=?", (kind,str(id))).fetchone()
        return json.loads(row[0]) if row else default

    def all(self, kind):
        return [json.loads(r[0]) for r in self.db.execute("SELECT data FROM documents WHERE kind=? ORDER BY id", (kind,))]

    def put(self, kind, id, data):
        with self.db:
            self.db.execute("INSERT INTO documents VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data", (kind,str(id),json.dumps(data)))
        return data

    def delete(self, kind, id):
        with self.db:
            self.db.execute("DELETE FROM documents WHERE kind=? AND id=?", (kind,str(id)))

    def credential(self, id):
        row = self.db.execute("SELECT value FROM credentials WHERE id=?", (id,)).fetchone()
        return row[0] if row else ""

    def set_credential(self, id, value):
        with self.db:
            self.db.execute("INSERT INTO credentials VALUES(?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value", (id,value))

    def event(self, type, message, session_id=None):
        now = time.time()
        with self.db:
            cursor = self.db.execute("INSERT INTO events(type,session_id,message,created) VALUES(?,?,?,?)", (type,session_id,message[:4000],now))
            # Bounded history; transcripts and audio are not an audit log.
            self.db.execute("DELETE FROM events WHERE id < (SELECT MAX(id)-10000 FROM events)")
        return {"id":cursor.lastrowid,"type":type,"session_id":session_id,"message":message[:4000],"created":now}

    def events(self, after=0, limit=100):
        if after:
            return [dict(row) for row in self.db.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (after,limit))]
        rows = self.db.execute("SELECT * FROM events WHERE id>? ORDER BY id DESC LIMIT ?", (after,limit)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def login(self, kind="session", lifetime=43200):
        token = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute("DELETE FROM logins WHERE expires<?", (time.time(),))
            self.db.execute("INSERT INTO logins VALUES(?,?,?)", (hashlib.sha256(token.encode()).hexdigest(),time.time()+lifetime,kind))
        return token

    def valid_login(self, token, kind="session", consume=False):
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = self.db.execute("SELECT expires,kind FROM logins WHERE digest=?", (digest,)).fetchone()
        valid = bool(row and row[0] > time.time() and row[1] == kind)
        if valid and consume:
            with self.db:
                self.db.execute("DELETE FROM logins WHERE digest=?", (digest,))
        return valid

    def revoke_login(self, token):
        with self.db:
            self.db.execute("DELETE FROM logins WHERE digest=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def contact(self, user_id, name, username="", updated=None):
        updated = time.time() if updated is None else updated
        with self.db:
            self.db.execute("""INSERT INTO contacts(user_id,name,username,updated) VALUES(?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET name=excluded.name,username=excluded.username,updated=excluded.updated
                WHERE excluded.updated >= contacts.updated""", (user_id,name,username,updated))

    def contact_name(self, user_id, name, username=""):
        """Enrich display metadata without inventing a new connection time."""
        with self.db:
            self.db.execute("UPDATE contacts SET name=?,username=? WHERE user_id=?",(name,username,user_id))

    def contacts(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM contacts WHERE id<=9999 ORDER BY updated DESC LIMIT 100")]

    def close(self):
        self.db.close()
