"""Run isolated review reproductions and optionally save auditable JSON evidence.

python3 -m tests.run_deepseek_reproduction --output /tmp/deepseek-evidence.json
Use a fixed SQLite runtime for case 08; legacy SQLite explicitly skips that case.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import patch

from tests import run_stability

ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    'app/db/__init__.py', 'app/db/settings.py', 'app/db/runtime.py',
    'app/db/transactions.py', 'app/db/main_db.py', 'app/db/media_db.py',
    'app/db/backup.py', 'app/db/models.py', 'app/mediaserver/media_server.py',
    'app/helper/subtitle_tasks.py', 'docker/Dockerfile',
    'db_scripts/versions/7e1c9a42b605_database_publications.py',
    'tests/test_database_migrations.py', 'scripts/verify_database_workload.py', 'run.py',
    'tests/test_deepseek_audit_reproduction.py', 'tests/run_deepseek_reproduction.py',
    'tests/test_database_review_fixes.py', 'scripts/database_maintenance.py',
)


def fingerprints():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    before = fingerprints()
    # Reuse the existing runner's disposable configuration, initialization and
    # network denial. Importing the reproduction module earlier would bypass it.
    module = 'tests.test_deepseek_audit_reproduction'
    with patch.object(sys, 'argv', ['run_stability', module]):
        status = run_stability.main()
    after = fingerprints()
    changed = [name for name in SOURCES if before[name] != after[name]]
    if changed:
        status = 1
    loaded = sys.modules.get(module)
    record = {
        'recorded_at': datetime.now(timezone.utc).isoformat(),
        'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'python': sys.version.split()[0], 'sqlite': sqlite3.sqlite_version,
        'exit_code': status, 'changed_during_run': changed,
        'source_sha256_before': before, 'source_sha256_after': after,
        'evidence': getattr(loaded, 'EVIDENCE', {}),
        'meaning': 'Cases 01/03/04/06/10/12 verify fixes; other cases verify review observations.',
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(record, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        print('Evidence saved:', args.output)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
