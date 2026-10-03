import sqlite3, uuid
from pathlib import Path
from contextlib import contextmanager
from app.config import subscription_secret, token_hash
from app.models.protocol import stamp

class Store:
    def __init__(self, directory, calendar_token=None):
        self.directory=Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.path=self.directory/'calendar.sqlite3'
        # Autocommit so PRAGMA foreign_keys can change outside a transaction.
        db=sqlite3.connect(self.path,timeout=3,isolation_level=None)
        db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA busy_timeout=3000')
            version=db.execute('PRAGMA user_version').fetchone()[0]
            if version==0:
                db.executescript((Path(__file__).parent/'001_initial.sql').read_text())
                db.execute('INSERT INTO settings(id,state_epoch) VALUES(1,?)',(str(uuid.uuid4()),))
                db.execute('PRAGMA user_version=1')
                version=1
            if version==1:
                _migrate_v1_to_v2(db, calendar_token)
                version=2
            if version==2:
                _migrate_v2_to_v3(db)
                version=3
            if version==3:
                _migrate_v3_to_v4(db)
                version=4
            if version==4:
                _migrate_v4_to_v5(db)
                version=5
            if version!=5:
                raise RuntimeError('Unsupported database version')
            _sync_public_calendar(db, calendar_token)
        finally:
            db.close()
        self.path.chmod(0o600)

    def connect(self):
        db=sqlite3.connect(self.path,timeout=3)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=3000')
        return db

    @contextmanager
    def tx(self, write=False):
        db=self.connect()
        try:
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally: db.close()

    def backup(self, force=False):
        folder=self.directory/'backups'
        folder.mkdir(exist_ok=True,mode=0o700)
        daily=folder/(stamp()[:10]+'.sqlite3')
        # The running service keeps one automatic copy per UTC day. A manual backup
        # still writes a new snapshot when that copy already exists.
        if daily.exists() and not force:
            self._prune_backups(folder)
            return daily
        target=daily
        if daily.exists():
            moment=stamp()
            target=folder/(moment[:10]+'T'+moment[11:19].replace(':','')+'Z.sqlite3')
            if target.exists(): target=folder/(target.stem+'-'+uuid.uuid4().hex[:8]+target.suffix)
        temporary=target.with_suffix('.tmp')
        with self.connect() as source, sqlite3.connect(temporary) as dest:
            source.backup(dest)
        temporary.chmod(0o600)
        temporary.replace(target)
        self._prune_backups(folder)
        return target

    def _prune_backups(self, folder):
        for old in sorted(folder.glob('*.sqlite3'))[:-7]: old.unlink()

    def vacuum(self):
        # VACUUM cannot run inside a transaction. Autocommit mode keeps it outside one.
        db=sqlite3.connect(self.path,timeout=3,isolation_level=None)
        try:
            db.execute('PRAGMA busy_timeout=3000')
            db.execute('VACUUM')
        finally: db.close()

def _migrate_v2_to_v3(db):
    db.execute('BEGIN')
    try:
        columns=[row[1] for row in db.execute('PRAGMA table_info(accounts)')]
        if 'login_token' not in columns:
            db.execute('ALTER TABLE accounts ADD COLUMN login_token TEXT')
        db.execute('PRAGMA user_version=3')
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise

def _migrate_v3_to_v4(db):
    db.execute('BEGIN')
    try:
        columns=[row[1] for row in db.execute('PRAGMA table_info(accounts)')]
        if 'login_token' in columns:
            db.execute('ALTER TABLE accounts DROP COLUMN login_token')
        db.execute('PRAGMA user_version=4')
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise

def _migrate_v4_to_v5(db):
    # A long path secret cannot be shortened in place. Replace it once; the old URL becomes 404.
    db.execute('BEGIN')
    try:
        for row in db.execute("SELECT id,calendar_token FROM accounts WHERE kind='invite'"):
            if len(row['calendar_token'])==16: continue
            secret=subscription_secret()
            digest=token_hash(secret)
            while db.execute('SELECT 1 FROM accounts WHERE calendar_hash=?',(digest,)).fetchone():
                secret=subscription_secret()
                digest=token_hash(secret)
            db.execute('UPDATE accounts SET calendar_hash=?,calendar_token=? WHERE id=?',(digest,secret,row['id']))
        db.execute('PRAGMA user_version=5')
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise

def _sync_public_calendar(db, calendar_token):
    if not calendar_token: return
    digest=token_hash(calendar_token)
    row=db.execute("SELECT id,calendar_hash FROM accounts WHERE kind='public'").fetchone()
    if not row or row['calendar_hash']==digest: return
    db.execute('BEGIN')
    try:
        db.execute('UPDATE accounts SET calendar_hash=?,calendar_token=? WHERE id=?',(digest,calendar_token,row['id']))
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise

def _migrate_v1_to_v2(db, calendar_token):
    db.execute('PRAGMA foreign_keys=OFF')
    db.execute('BEGIN')
    try:
        public_id=str(uuid.uuid4())
        token=calendar_token or subscription_secret()
        old=db.execute('SELECT * FROM settings').fetchone()
        db.execute('''CREATE TABLE accounts (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('public','invite')),
            label TEXT NOT NULL DEFAULT '',
            login_hash TEXT,
            calendar_hash TEXT NOT NULL,
            calendar_token TEXT NOT NULL,
            revoked_at TEXT,
            created_at TEXT NOT NULL)''')
        db.execute('INSERT INTO accounts(id,kind,label,login_hash,calendar_hash,calendar_token,revoked_at,created_at) VALUES(?,?,?,?,?,?,NULL,?)',
            (public_id,'public','公开日历',None,token_hash(token),token,stamp()))
        db.execute('CREATE UNIQUE INDEX accounts_one_public ON accounts(kind) WHERE kind=\'public\'')
        db.execute('CREATE UNIQUE INDEX accounts_login_hash ON accounts(login_hash) WHERE login_hash IS NOT NULL')
        db.execute('CREATE UNIQUE INDEX accounts_calendar_hash ON accounts(calendar_hash)')
        db.execute('''CREATE TABLE settings_v2 (
            account_id TEXT PRIMARY KEY REFERENCES accounts(id),
            state_epoch TEXT NOT NULL,
            config_version INTEGER NOT NULL DEFAULT 1,
            data_revision INTEGER NOT NULL DEFAULT 0,
            active_publication_id TEXT,
            schema_version INTEGER NOT NULL DEFAULT 1,
            publish_error TEXT)''')
        db.execute('''INSERT INTO settings_v2(account_id,state_epoch,config_version,data_revision,active_publication_id,schema_version,publish_error)
            VALUES(?,?,?,?,?,?,?)''',(public_id,old['state_epoch'],old['config_version'],old['data_revision'],old['active_publication_id'],old['schema_version'],old['publish_error']))
        db.execute('DROP TABLE settings')
        db.execute('ALTER TABLE settings_v2 RENAME TO settings')
        _replace_table(db,'interests','''CREATE TABLE interests_v2 (
            id TEXT PRIMARY KEY,
            keyword TEXT NOT NULL,
            conditions TEXT NOT NULL,
            normalized_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            deleted_at TEXT,
            account_id TEXT NOT NULL REFERENCES accounts(id))''',
            'INSERT INTO interests_v2(id,keyword,conditions,normalized_key,created_at,deleted_at,account_id) SELECT id,keyword,conditions,normalized_key,created_at,deleted_at,? FROM interests',(public_id,))
        db.execute('CREATE UNIQUE INDEX active_interest ON interests(account_id,normalized_key) WHERE deleted_at IS NULL')
        _replace_table(db,'events','''CREATE TABLE events_v2 (
            id TEXT PRIMARY KEY,
            uid TEXT UNIQUE NOT NULL,
            version INTEGER NOT NULL,
            sequence INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            calendar_visible INTEGER NOT NULL,
            withdrawal_reason TEXT,
            ics_modified_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            account_id TEXT NOT NULL REFERENCES accounts(id))''',
            'INSERT INTO events_v2(id,uid,version,sequence,payload_json,calendar_visible,withdrawal_reason,ics_modified_at,created_at,updated_at,account_id) SELECT id,uid,version,sequence,payload_json,calendar_visible,withdrawal_reason,ics_modified_at,created_at,updated_at,? FROM events',(public_id,))
        _replace_table(db,'event_sources','''CREATE TABLE event_sources_v2 (
            source_namespace TEXT NOT NULL,
            source_id TEXT NOT NULL,
            event_id TEXT NOT NULL REFERENCES events(id),
            source_url TEXT NOT NULL,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            PRIMARY KEY(account_id,source_namespace,source_id))''',
            'INSERT INTO event_sources_v2(source_namespace,source_id,event_id,source_url,account_id) SELECT source_namespace,source_id,event_id,source_url,? FROM event_sources',(public_id,))
        _replace_table(db,'candidates','''CREATE TABLE candidates_v2 (
            id TEXT PRIMARY KEY,
            client_candidate_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            state TEXT NOT NULL,
            resolved_at TEXT,
            resolution TEXT,
            resolved_event_id TEXT,
            account_id TEXT NOT NULL REFERENCES accounts(id))''',
            'INSERT INTO candidates_v2(id,client_candidate_id,version,payload_json,state,resolved_at,resolution,resolved_event_id,account_id) SELECT id,client_candidate_id,version,payload_json,state,resolved_at,resolution,resolved_event_id,? FROM candidates',(public_id,))
        db.execute('CREATE UNIQUE INDEX candidates_client_id ON candidates(account_id,client_candidate_id)')
        _replace_table(db,'batches','''CREATE TABLE batches_v2 (
            batch_id TEXT PRIMARY KEY,
            state_epoch TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            config_version INTEGER,
            status INTEGER NOT NULL,
            target_revision INTEGER,
            receipt_json TEXT NOT NULL,
            received_at TEXT NOT NULL,
            account_id TEXT NOT NULL REFERENCES accounts(id))''',
            'INSERT INTO batches_v2(batch_id,state_epoch,payload_hash,config_version,status,target_revision,receipt_json,received_at,account_id) SELECT batch_id,state_epoch,payload_hash,config_version,status,target_revision,receipt_json,received_at,? FROM batches',(public_id,))
        _replace_table(db,'publications','''CREATE TABLE publications_v2 (
            id TEXT PRIMARY KEY,
            data_revision INTEGER NOT NULL,
            ics_blob BLOB NOT NULL,
            sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            account_id TEXT NOT NULL REFERENCES accounts(id))''',
            'INSERT INTO publications_v2(id,data_revision,ics_blob,sha256,created_at,account_id) SELECT id,data_revision,ics_blob,sha256,created_at,? FROM publications',(public_id,))
        db.execute('PRAGMA user_version=2')
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise
    db.execute('PRAGMA foreign_keys=ON')

def _replace_table(db, name, create_sql, insert_sql, params):
    db.execute(create_sql)
    db.execute(insert_sql, params)
    db.execute(f'DROP TABLE {name}')
    db.execute(f'ALTER TABLE {name}_v2 RENAME TO {name}')
