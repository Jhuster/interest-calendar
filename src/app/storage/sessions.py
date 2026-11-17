"""Browser sessions are persisted separately from restorable calendar content."""
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

SESSION_MAX_AGE = 30 * 24 * 3600


class SessionStore:
    def __init__(self, directory):
        self.path = Path(directory) / 'sessions.sqlite3'

    def initialize(self):
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS sessions (id_hash TEXT PRIMARY KEY, expires REAL NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires)')
            db.execute('DELETE FROM sessions WHERE expires<=?', (time.time(),))

    @staticmethod
    def key(sid):
        return hashlib.sha256(sid.encode()).hexdigest()

    def get(self, sid):
        if not sid:
            return None
        with sqlite3.connect(self.path) as db:
            row = db.execute('SELECT expires,payload FROM sessions WHERE id_hash=?', (self.key(sid),)).fetchone()
            if row is None:
                return None
            if row[0] <= time.time():
                db.execute('DELETE FROM sessions WHERE id_hash=?', (self.key(sid),))
                return None
            return json.loads(row[1])

    def __setitem__(self, sid, session):
        with sqlite3.connect(self.path) as db:
            db.execute('DELETE FROM sessions WHERE expires<=?', (time.time(),))
            db.execute('INSERT INTO sessions VALUES(?,?,?)', (self.key(sid), session['expires'], json.dumps(session)))

    def pop(self, sid, default=None):
        if sid:
            with sqlite3.connect(self.path) as db:
                db.execute('DELETE FROM sessions WHERE id_hash=?', (self.key(sid),))


def invalidate_sessions(directory):
    # Restore holds the service lock, so no service can have this database open.
    (Path(directory) / 'sessions.sqlite3').unlink(missing_ok=True)
