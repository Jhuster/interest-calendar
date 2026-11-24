"""Daily aggregate usage only: no URLs, IP addresses, tokens or calendar contents."""
import os
import hashlib
import sqlite3
from datetime import timedelta
from contextlib import contextmanager
from pathlib import Path
from app.models.protocol import now, stamp, TZ

RETENTION_DAYS = 90


class UsageStore:
    def __init__(self, directory):
        self.path = Path(directory) / 'usage.sqlite3'

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=0.2)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute("INSERT OR IGNORE INTO metadata VALUES('started_at',?)", (stamp(),))
            db.execute('''CREATE TABLE IF NOT EXISTS daily_usage (
                day TEXT NOT NULL, account_id TEXT NOT NULL, is_public INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 0, subscriptions INTEGER NOT NULL DEFAULT 0,
                not_modified INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(day,account_id))''')
            db.execute("INSERT OR IGNORE INTO metadata VALUES('traffic_started_at',?)", (stamp(),))
            db.execute('CREATE TABLE IF NOT EXISTS website_daily (day TEXT PRIMARY KEY, page_views INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS daily_visitors (day TEXT NOT NULL, visitor_hash TEXT NOT NULL, PRIMARY KEY(day,visitor_hash))')
            self.prune(db)

    def prune(self, db):
        earliest = now().astimezone(TZ).date() - timedelta(days=RETENTION_DAYS - 1)
        db.execute('DELETE FROM daily_usage WHERE day<?', (str(earliest),))
        db.execute('DELETE FROM website_daily WHERE day<?', (str(earliest),))
        db.execute('DELETE FROM daily_visitors WHERE day<?', (str(earliest),))

    def record(self, account_id, *, is_public, active=False, subscription=False, not_modified=False):
        day = str(now().astimezone(TZ).date())
        with self.connect() as db:
            self.prune(db)
            db.execute('''INSERT INTO daily_usage VALUES(?,?,?,?,?,?)
                ON CONFLICT(day,account_id) DO UPDATE SET
                active=max(active,excluded.active),
                subscriptions=subscriptions+excluded.subscriptions,
                not_modified=not_modified+excluded.not_modified''',
                (day, account_id, int(is_public), int(active), int(subscription), int(not_modified)))

    def snapshot(self, first, last):
        with self.connect() as db:
            started = db.execute("SELECT value FROM metadata WHERE key='started_at'").fetchone()[0]
            rows = [dict(row) for row in db.execute('SELECT * FROM daily_usage WHERE day>=? AND day<=?', (first, last))]
        return started, rows

    def record_pageview(self, visitor):
        day = str(now().astimezone(TZ).date())
        digest = hashlib.sha256(visitor.encode()).hexdigest()
        with self.connect() as db:
            self.prune(db)
            db.execute('INSERT INTO website_daily VALUES(?,1) ON CONFLICT(day) DO UPDATE SET page_views=page_views+1', (day,))
            db.execute('INSERT OR IGNORE INTO daily_visitors VALUES(?,?)', (day, digest))

    def traffic_snapshot(self, first, last):
        with self.connect() as db:
            db.execute('BEGIN')
            started = db.execute("SELECT value FROM metadata WHERE key='traffic_started_at'").fetchone()[0]
            views = {r['day']:r['page_views'] for r in db.execute('SELECT * FROM website_daily WHERE day>=? AND day<=?', (first,last))}
            visitors = {r['day']:r['count'] for r in db.execute('SELECT day,count(*) AS count FROM daily_visitors WHERE day>=? AND day<=? GROUP BY day', (first,last))}
            unique = db.execute('SELECT count(DISTINCT visitor_hash) FROM daily_visitors WHERE day>=? AND day<=?', (first,last)).fetchone()[0]
        return started, views, visitors, unique
