"""Durable, opt-in reconciliation from adopted sources to external readings.

Control remains the authority for source identity. This separate publication
journal owns only export progress; it never changes successful Capture outcomes.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid

from ..sources.adoption import decode_engine_ref
from .files import PublicationConflict, digest, directory, inventory, parts, read_file, sync_tree, write_private
from .sandbox import ReadingsBoundaryError


class ReadingsService:
    def __init__(self, *, root: Path, state: Path, corpus: Path, store):
        self.root, self.state, self.corpus = root, state, corpus.resolve(strict=True)
        self.store = store
        with directory(root) as fd:
            info = os.fstat(fd)
            self.identity = [info.st_dev, info.st_ino]
        for other in (state.resolve(), self.corpus):
            if root.is_relative_to(other) or other.is_relative_to(root):
                raise ReadingsBoundaryError('readings roots must be disjoint')
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        if state.stat().st_dev != info.st_dev:
            raise ReadingsBoundaryError('readings staging and library must share a filesystem')
        self.database = state / 'publication.sqlite3'
        self._stop = threading.Event()
        self._thread = None
        self._child = None
        self._root_lock_fd = None
        self._child_lock = threading.Lock()
        self.last_failure = None
        with self.lock(), self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS baseline (source_id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS publications (
                    source_id TEXT PRIMARY KEY, canonical_id TEXT NOT NULL, paper_dir TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', failure TEXT,
                    owned TEXT NOT NULL DEFAULT '{}', manifest TEXT, task TEXT,
                    target_identity TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0);
            ''')
            bound = db.execute("SELECT value FROM settings WHERE key='root'").fetchone()
            binding = json.dumps([str(root), self.identity, str(self.corpus)])
            if bound is None:
                db.execute("INSERT INTO settings VALUES ('root',?)", (binding,))
                db.execute("INSERT INTO settings VALUES ('schema_version','1')")
                # Existing adopted sources are not a bulk-backfill instruction.
                db.executemany('INSERT INTO baseline VALUES (?)', [
                    (s['id'],) for s in store.list_sources()
                    if s['import_state'] in ('existing', 'imported') and s.get('engine_ref')
                ])
            elif bound[0] != binding:
                raise ReadingsBoundaryError('readings root or corpus binding changed; use a new publication state')
            else:
                version = db.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
                if version is None or version[0] != '1':
                    raise ReadingsBoundaryError('unsupported publication journal schema')

    @contextmanager
    def connect(self):
        with directory(self.state):
            pass
        if os.path.lexists(self.database):
            info = self.database.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ReadingsBoundaryError('unsafe publication journal')
        db = sqlite3.connect(self.database, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def lock(self):
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.state / 'publisher.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # A directory lock also serializes separate installations targeting
            # the same library, without creating a lock file inside that library.
            with directory(self.root) as root_fd:
                fcntl.flock(root_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._root_lock_fd = root_fd
                try:
                    yield
                finally:
                    self._root_lock_fd = None
        finally:
            os.close(fd)

    def track(self, source: dict) -> None:
        if (source['import_state'] not in ('existing', 'imported')
                or not str(source.get('engine_ref') or '').startswith('paper:')):
            return
        name = decode_engine_ref(source['engine_ref'])
        if len(parts(name)) != 1:
            raise PublicationConflict('invalid_paper_directory')
        with self.connect() as db:
            row = db.execute('SELECT canonical_id,paper_dir FROM publications WHERE source_id=?', (source['id'],)).fetchone()
            if row and (row['canonical_id'], row['paper_dir']) != (source['canonical_id'], name):
                raise PublicationConflict('source_binding_changed')
            db.execute('INSERT OR IGNORE INTO publications (source_id,canonical_id,paper_dir) VALUES (?,?,?)',
                       (source['id'], source['canonical_id'], name))

    def retry(self, source_id: str) -> None:
        with self.lock():
            self.track(self.store.get_source(source_id))
            with self.connect() as db:
                db.execute("UPDATE publications SET state='pending',failure=NULL,next_attempt=0 WHERE source_id=?", (source_id,))

    def status(self) -> dict:
        with self.connect() as db:
            items = [dict(r) for r in db.execute('SELECT source_id,canonical_id,paper_dir,state,failure,attempts,updated_at FROM publications ORDER BY source_id')]
        return {'enabled': True, 'items': items, 'failure': self.last_failure,
                'index_refresh': 'external', 'pdf_download': 'not_requested'}

    def _prepare(self, row, hashes: dict[str, str]) -> dict:
        target = self.root / row['paper_dir']
        source = self.corpus / row['paper_dir']
        new_directory = not os.path.lexists(target)
        target_identity = None
        owned = json.loads(row['owned'])
        if not new_directory:
            with directory(target) as fd:
                info = os.fstat(fd)
                target_identity = [info.st_dev, info.st_ino]
            if row['target_identity'] and json.loads(row['target_identity']) != target_identity:
                raise PublicationConflict('paper_directory_replaced')
            if not owned:
                # A title/path match never establishes identity in a legacy library.
                notes = read_file(target, 'notes.md').decode('utf-8')
                canonical = row['canonical_id']
                if not canonical.startswith('arxiv:'):
                    raise PublicationConflict('unverified_existing_paper')
                identifier = re.escape(canonical.removeprefix('arxiv:'))
                if not re.search(r'(?:arxiv:|arxiv\.org/(?:abs|pdf|html)/)' + identifier + r'(?:v\d+)?(?![\w.])', notes):
                    raise PublicationConflict('unverified_existing_paper')
        stage = self.state / 'staging' / uuid.uuid4().hex
        (stage / 'new').mkdir(parents=True, mode=0o700)
        files = {}
        conflicts = []
        missing_dirs = set()
        for name, checksum in hashes.items():
            data = read_file(source, name)
            if digest(data) != checksum:
                raise PublicationConflict('source_changed_during_staging')
            old = None
            if not new_directory:
                try:
                    old_data = read_file(target, name)
                    old = digest(old_data)
                except FileNotFoundError:
                    pass
            if old is None:
                action = 'create'
            elif Path(name).name.casefold() == 'notes.md':
                action = 'preserve'
            elif old == checksum:
                action = 'preserve'
            elif owned.get(name) == old:
                action = 'update'
                write_private(stage / 'before' / name, old_data)
            else:
                action = 'preserve'
                conflicts.append(name)
            write_private(stage / 'new' / name, data)
            files[name] = {'action': action, 'old': old, 'new': checksum}
            if not new_directory:
                for parent in Path(name).parents:
                    if parent == Path('.'):
                        continue
                    if not os.path.lexists(target / parent):
                        missing_dirs.add(parent.as_posix())
        # Publish only the highest missing directory, containing its entire subtree.
        directories = sorted(p for p in missing_dirs if not any(q != p and p.startswith(q + '/') for q in missing_dirs))
        for relative in directories:
            destination = stage / 'directories' / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            (stage / 'new' / relative).rename(destination)
        creates = [row['paper_dir']] if new_directory else [
            f"{row['paper_dir']}/{name}" for name, value in files.items() if value['action'] == 'create'
        ]
        creates.extend(f"{row['paper_dir']}/{name}" for name in directories)
        sync_tree(stage)
        return {'root': str(self.root), 'root_identity': self.identity, 'target_identity': target_identity,
                'paper_dir': row['paper_dir'], 'stage': str(stage), 'new_directory': new_directory,
                'directories': directories, 'creates': creates, 'files': files, 'conflicts': conflicts}

    def _run(self, task: dict) -> dict:
        with self._child_lock:
            if self._stop.is_set():
                return {'state': 'failed', 'failure': 'publisher_stopping'}
            self._child = subprocess.Popen(
                [sys.executable, '-I', '-B', '-m', 'cortex_platform.product.readings.publisher'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env={'PATH': os.defpath, 'HOME': str(self.state), 'LANG': 'C.UTF-8'}, close_fds=True,
                # Retain the shared root lock if the controller dies mid-write.
                pass_fds=(self._root_lock_fd,),
            )
        try:
            stdout, _ = self._child.communicate(json.dumps(task), timeout=120)
            result = json.loads(stdout)
            if result.get('state') not in ('published', 'failed', 'conflict'):
                raise ValueError('invalid publisher result')
            return result
        except (subprocess.TimeoutExpired, ValueError):
            self._child.kill()
            self._child.communicate()
            return {'state': 'failed', 'failure': 'publisher_interrupted'}
        finally:
            self._child = None

    def tick(self) -> None:
        with self.lock():
            with directory(self.root) as fd:
                info = os.fstat(fd)
                if [info.st_dev, info.st_ino] != self.identity:
                    raise ReadingsBoundaryError('readings root identity changed')
            with self.connect() as db:
                baseline = {r[0] for r in db.execute('SELECT source_id FROM baseline')}
            for source in self.store.list_sources():
                if source['id'] not in baseline:
                    self.track(source)
            with self.connect() as db:
                rows = db.execute('SELECT * FROM publications ORDER BY updated_at,source_id').fetchall()
            # Bounded per tick, with independent retry backoff for publication failures.
            attempted = 0
            for row in rows:
                if self._stop.is_set() or attempted >= 8:
                    break
                if row['state'] == 'conflict' or row['next_attempt'] > time.time():
                    continue
                attempted += 1
                try:
                    if row['task']:
                        task = json.loads(row['task'])
                        hashes = json.loads(row['manifest'])
                    else:
                        hashes = inventory(self.corpus / row['paper_dir'])
                        if row['state'] == 'published' and json.loads(row['manifest']) == hashes:
                            attempted -= 1
                            continue
                        task = self._prepare(row, hashes)
                        with self.connect() as db:
                            db.execute("UPDATE publications SET task=?,manifest=?,state='publishing' WHERE source_id=?",
                                       (json.dumps(task), json.dumps(hashes), row['source_id']))
                    result = self._run(task)
                    owned = json.loads(row['owned'])
                    if result['state'] == 'published':
                        for name, value in task['files'].items():
                            if value['action'] in ('create', 'update') and Path(name).name.casefold() != 'notes.md':
                                owned[name] = value['new']
                        with self.connect() as db:
                            conflicts = task.get('conflicts', [])
                            db.execute("UPDATE publications SET state=?,failure=?,owned=?,target_identity=?,task=NULL,attempts=0,next_attempt=0,updated_at=? WHERE source_id=?",
                                       ('conflict' if conflicts else 'published',
                                        'protected_modified_files:' + ','.join(conflicts) if conflicts else None,
                                        json.dumps(owned), json.dumps(result['target_identity']), time.time(), row['source_id']))
                        continue
                except (OSError, ValueError, sqlite3.Error) as error:
                    result = {'state': 'conflict' if isinstance(error, PublicationConflict) else 'failed',
                              'failure': str(error) if isinstance(error, PublicationConflict) else type(error).__name__}
                with self.connect() as db:
                    attempts = row['attempts'] + 1
                    db.execute('UPDATE publications SET state=?,failure=?,attempts=?,next_attempt=?,updated_at=? WHERE source_id=?',
                               (result['state'], result['failure'], attempts, time.time() + min(3600, 30 * 2 ** min(attempts, 7)), time.time(), row['source_id']))

    def start(self) -> None:
        if self._thread is not None:
            return
        def loop():
            while not self._stop.is_set():
                try:
                    self.tick()
                    self.last_failure = None
                except (OSError, ValueError, sqlite3.Error) as error:
                    self.last_failure = type(error).__name__
                    print(f'readings publication tick failed: {type(error).__name__}', file=sys.stderr, flush=True)
                self._stop.wait(30)
        self._thread = threading.Thread(target=loop, name='readings-publication', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._child_lock:
            child = self._child
            if child is not None and child.poll() is None:
                child.kill()
        if self._thread is not None:
            self._thread.join(timeout=5)


def configured_root(config: dict, paths) -> Path | None:
    section = config.get('readings')
    if not section:
        return None
    if sys.platform != 'darwin':
        raise ReadingsBoundaryError('readings publication requires macOS')
    root = Path(section['papers_root'])
    if root != root.resolve(strict=True):
        raise ReadingsBoundaryError('readings root must be a canonical path without symlinks')
    with directory(root):
        pass
    for path in paths.directories():
        path = path.resolve()
        if root.is_relative_to(path) or path.is_relative_to(root):
            raise ReadingsBoundaryError('readings root must be outside product directories')
    return root


def build_readings_service(*, config, paths, store) -> ReadingsService | None:
    root = configured_root(config, paths)
    if root is None:
        return None
    corpus = store.get_asset_root('research-corpus')
    if not corpus.enabled:
        raise ReadingsBoundaryError('research corpus is disabled')
    return ReadingsService(root=root, state=paths.state_dir / 'readings', corpus=Path(corpus.private_path), store=store)


def read_status(paths, *, enabled: bool) -> dict:
    """Read publication status without initializing state or acquiring a writer lock."""
    database = paths.state_dir / 'readings' / 'publication.sqlite3'
    if not enabled or not database.exists():
        return {'enabled': enabled, 'items': [], 'state': 'not_started' if enabled else 'disabled'}
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5) as db:
        db.row_factory = sqlite3.Row
        items = [dict(r) for r in db.execute('SELECT source_id,canonical_id,paper_dir,state,failure,attempts,updated_at FROM publications ORDER BY source_id')]
    return {'enabled': True, 'items': items, 'index_refresh': 'external', 'pdf_download': 'not_requested'}
