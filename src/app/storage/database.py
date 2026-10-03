import sqlite3, uuid
from pathlib import Path
from contextlib import contextmanager
from app.models.protocol import stamp

class Store:
    def __init__(self, directory):
        self.directory=Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.path=self.directory/'calendar.sqlite3'
        with self.connect() as db:
            version=db.execute('PRAGMA user_version').fetchone()[0]
            if version==0:
                db.executescript((Path(__file__).parent/'001_initial.sql').read_text())
                db.execute('INSERT INTO settings(id,state_epoch) VALUES(1,?)',(str(uuid.uuid4()),))
                db.execute('PRAGMA user_version=1')
            elif version!=1: raise RuntimeError('Unsupported database version')
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
