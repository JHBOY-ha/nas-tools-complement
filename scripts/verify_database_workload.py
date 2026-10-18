"""Compare the checked-in 8d84b13 baseline and current audit paths.

The optional --scratch-root explicitly selects a test volume. Never open live
configuration/databases. SQLite must contain the WAL-reset fix for acceptance.
"""
import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import types


def percentile(values):
    values = sorted(values)
    return round(values[min(len(values) - 1, int(len(values) * .95))] * 1000, 3) if values else None


BASELINE_REVISION = '8d84b13'
BASELINE_ROOT = Path(__file__).resolve().parent / 'baselines' / BASELINE_REVISION


def load_baseline(_root=None):
    """Load the immutable benchmark snapshot without consulting Git history."""
    manifest_path = BASELINE_ROOT / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('revision') != BASELINE_REVISION:
        raise RuntimeError('基准快照版本清单不匹配：%s' % BASELINE_REVISION)

    def source(path):
        file_path = BASELINE_ROOT / path
        value = file_path.read_text(encoding='utf-8')
        expected = manifest['files'].get(path)
        actual = hashlib.sha256(value.encode('utf-8')).hexdigest()
        if expected != actual:
            raise RuntimeError('基准快照校验失败：%s' % path)
        return value

    models = types.ModuleType('_nas_db_baseline_models')
    exec(compile(source('app/db/models.py'), '8d84b13/models.py', 'exec'), models.__dict__)
    sys.modules[models.__name__] = models
    tree = ast.parse(source('app/helper/subtitle_tasks.py'))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == 'app.db.models':
            node.module = models.__name__
    namespace = {'__name__': '_nas_db_baseline_tasks'}
    # Execute the actual immutable manager, replacing only its injected model
    # module. No monkeypatched/no-op atomicity, locks or commits are compared.
    exec(compile(ast.fix_missing_locations(tree), '8d84b13/subtitle_tasks.py', 'exec'), namespace)
    return models, namespace['SubtitleTaskManager']


def trial(path, rows, readers, writers, baseline):
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import scoped_session, sessionmaker
    from sqlalchemy.pool import QueuePool
    from app.db.models import Base, SUBTITLETASK
    from app.db.transactions import ManagedDatabase, WriteCoordinator
    from app.db.settings import DatabaseSettings
    from app.db.publication import seed_legacy, filter_visible
    from app.helper.subtitle_tasks import SubtitleTaskManager
    models, manager_class = load_baseline() if baseline else (None, SubtitleTaskManager)
    task_model = models.SUBTITLETASK if baseline else SUBTITLETASK
    metadata = models.Base.metadata if baseline else Base.metadata
    settings = DatabaseSettings(reserve_free_mb=256)
    engine = create_engine('sqlite:///' + str(path), poolclass=QueuePool, pool_size=20, max_overflow=30,
                           connect_args={'check_same_thread': False, 'timeout': 30})
    write_engine = engine if baseline else create_engine('sqlite:///' + str(path), poolclass=QueuePool,
        pool_size=1, max_overflow=0, connect_args={'check_same_thread': False, 'timeout': 30})
    managed = None if baseline else ManagedDatabase(engine, write_engine, settings,
        WriteCoordinator(settings), str(path))
    reads = scoped_session(sessionmaker(bind=engine, expire_on_commit=False)) if baseline else None
    class Store:
        @property
        def session(self):
            return reads() if baseline else managed.session
        def query(self, *objects):
            query = self.session.query(*objects)
            return query if baseline else filter_visible(query, objects)
        def insert(self, value): self.session.add(value)
        def flush(self): self.session.flush()
        def commit(self): self.session.commit() if baseline else managed.commit()
        def rollback(self): self.session.rollback() if baseline else managed.rollback()
        def remove_session(self): reads.remove() if baseline else managed.remove_session()
        @contextmanager
        def write_transaction(self, required_bytes=0):
            if baseline:
                try:
                    yield self.session
                    self.session.commit()
                except BaseException:
                    self.session.rollback(); raise
            else:
                with managed.write_transaction(required_bytes=required_bytes) as session: yield session
    db = Store()
    connection = engine.connect() if baseline else write_engine.connect()
    try:
        if not baseline:
            connection.exec_driver_sql('PRAGMA journal_mode=WAL')
        connection.exec_driver_sql('PRAGMA synchronous=FULL')
        metadata.create_all(connection)
        if not baseline:
            with connection.begin(): seed_legacy(connection)
    finally:
        connection.close()
    with db.write_transaction():
        for name in ['audit'] + ['writer-%s' % i for i in range(writers)]:
            db.insert(task_model(ID=name, TYPE='audit', OWNER='test', STATUS='running',
                                 CREATED_AT=time.time(), UPDATED_AT=time.time()))
    manager = manager_class(db=db, staging_root=str(path) + '.staging')
    timings = {'read': [], 'write': []}
    failures = []
    audit_started, audit_finished = threading.Event(), threading.Event()
    commits = {'count': 0}
    audit_metrics = {'thread': None, 'commits': 0, 'seconds': None}
    def count_commit(_connection):
        commits['count'] += 1
        if threading.get_ident() == audit_metrics['thread']:
            audit_metrics['commits'] += 1
    def signal(_connection, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith('INSERT') and 'SUBTITLE_AUDIT_STATE' in statement.upper():
            audit_started.set()
    event.listen(write_engine, 'commit', count_commit)
    event.listen(write_engine, 'before_cursor_execute', signal)
    def read():
        started = time.perf_counter()
        try: manager.get_task('audit', admin=True)
        finally:
            db.remove_session(); timings['read'].append(time.perf_counter() - started)
    def write(number, iteration):
        started = time.perf_counter()
        try:
            with db.write_transaction():
                row = db.query(task_model).filter(task_model.ID == 'writer-%s' % number).one()
                # Every measured call MUST dirty a row. Reassigning the same
                # value mostly measures read-only commits on the legacy path
                # and produces a misleading "write" latency comparison.
                row.MESSAGE = 'normal write %s' % iteration
                db.commit()
        finally:
            db.remove_session(); timings['write'].append(time.perf_counter() - started)
    def audit():
        audit_metrics['thread'] = threading.get_ident()
        audit_begin = time.perf_counter()
        try:
            manager.commit_audit_result('audit', 'scope', 'emby',
                {'/benchmark/%s.mkv' % i: {'status': 'ok'} for i in range(rows)}, {}, 'succeeded', 'done')
        except Exception as error:
            failures.append(type(error).__name__)
        finally:
            # Separate the full synthetic result-commit path from the overlap
            # window, which can include ordinary calls after audit publication.
            audit_metrics['seconds'] = round(time.perf_counter() - audit_begin, 3)
            audit_metrics['thread'] = None
            db.remove_session(); audit_finished.set()
    def caller(kind, number=0):
        if not audit_started.wait(30):
            failures.append('audit did not stage'); return
        for iteration in range(20):
            try: read() if kind == 'read' else write(number, iteration)
            except Exception as error: failures.append(type(error).__name__)
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=readers + writers + 1) as pool:
        futures = [pool.submit(caller, 'read') for _ in range(readers)]
        futures += [pool.submit(caller, 'write', i) for i in range(writers)]
        futures.append(pool.submit(audit))
        for future in futures: future.result(120)
    report = {'audit_window_read_p95_ms': percentile(timings['read']),
              'audit_window_write_p95_ms': percentile(timings['write']),
              'window_elapsed_seconds': round(time.perf_counter() - started, 3),
              'audit_commit_elapsed_seconds': audit_metrics['seconds'],
              'audit_transaction_commits': audit_metrics['commits'],
              'commits': commits['count'], 'errors': failures,
              'db_bytes': path.stat().st_size,
              'wal_bytes': Path(str(path) + '-wal').stat().st_size if Path(str(path) + '-wal').exists() else 0}
    timings = {'read': [], 'write': []}
    with ThreadPoolExecutor(max_workers=readers + writers) as pool:
        futures = [pool.submit(lambda: [read() for _ in range(20)]) for _ in range(readers)]
        futures += [pool.submit(lambda n=i: [write(n, iteration) for iteration in range(20)]) for i in range(writers)]
        for future in futures: future.result(60)
    report.update(idle_read_p95_ms=percentile(timings['read']), idle_write_p95_ms=percentile(timings['write']))
    with write_engine.connect() as connection:
        report['integrity'] = connection.exec_driver_sql('PRAGMA integrity_check').scalar()
        report['foreign_key_errors'] = len(connection.exec_driver_sql('PRAGMA foreign_key_check').all())
    db.remove_session(); engine.dispose()
    if write_engine is not engine: write_engine.dispose()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scratch-root', type=Path)
    parser.add_argument('--rows', type=int, default=50000)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--readers', type=int, default=10)
    parser.add_argument('--writers', type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.rows <= 50000 or not 1 <= args.rounds <= 5 \
            or not 1 <= args.readers <= 20 or not 1 <= args.writers <= 20:
        parser.error('Use 1–50000 rows, 1–5 rounds, and 1–20 readers/writers')
    version = sqlite3.sqlite_version_info
    if not (version >= (3, 51, 3) or version[:2] == (3, 50) and version >= (3, 50, 7)
            or version[:2] == (3, 44) and version >= (3, 44, 6)):
        parser.error('WAL measurement requires a fixed SQLite runtime')
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    with tempfile.TemporaryDirectory(prefix='nas-db-workload-', dir=args.scratch_root) as directory:
        config = Path(directory) / 'config.yaml'
        config.write_text('app: {}\nmedia: {}\npt: {}\nllm:\n  enable: false\n')
        os.environ['NASTOOL_CONFIG'] = str(config)
        os.environ['NASTOOL_OFFLINE_TESTS'] = '1'
        # This import installs only test dependency stubs; all measured SQLite,
        # locks, manager code and commits remain real.
        import tests.test_subtitle_tasks
        reports = {'sqlite': sqlite3.sqlite_version, 'rows': args.rows, 'readers': args.readers,
                   'writers': args.writers, 'scratch_parent': str(Path(directory).parent),
                   'baseline': [], 'current': []}
        for number in range(args.rounds):
            for name in ('baseline', 'current'):
                reports[name].append(trial(Path(directory) / ('%s-%s.db' % (name, number)),
                    args.rows, args.readers, args.writers, name == 'baseline'))
        metrics = ('audit_window_read_p95_ms', 'audit_window_write_p95_ms', 'idle_read_p95_ms', 'idle_write_p95_ms')
        medians = {name: {metric: statistics.median(item[metric] for item in reports[name]) for metric in metrics}
                   for name in ('baseline', 'current')}
        measured = all(value is not None for name in medians for value in medians[name].values())
        gates = {'read_improved': measured and medians['current'][metrics[0]] < medians['baseline'][metrics[0]],
                 'write_improved': measured and medians['current'][metrics[1]] < medians['baseline'][metrics[1]],
                 'idle_read_within_10_percent': measured and medians['current'][metrics[2]] <= medians['baseline'][metrics[2]] * 1.1,
                 'idle_write_within_10_percent': measured and medians['current'][metrics[3]] <= medians['baseline'][metrics[3]] * 1.1,
                 'integrity_and_no_errors': all(not item['errors'] and item['integrity'] == 'ok' and not item['foreign_key_errors']
                    for name in ('baseline', 'current') for item in reports[name])}
        reports.update(medians=medians, gates=gates, accepted=all(gates.values()))
        print(json.dumps(reports, ensure_ascii=False, indent=2))
        return 0 if reports['accepted'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
