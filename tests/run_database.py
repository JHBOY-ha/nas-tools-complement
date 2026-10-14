"""Run complete database acceptance; unsupported WAL runtimes are a hard failure.

Use --legacy-only to explicitly validate the safe DELETE fallback. This is not
an alternative to the mandatory WAL acceptance before enabling it in production.
"""
import argparse
import sqlite3
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--legacy-only', action='store_true')
    args = parser.parse_args()
    version = sqlite3.sqlite_version_info
    fixed = (version >= (3, 51, 3) or version[:2] == (3, 50) and version >= (3, 50, 7)
             or version[:2] == (3, 44) and version >= (3, 44, 6))
    if not args.legacy_only and not fixed:
        print('WAL acceptance requires a fixed SQLite runtime; current:', sqlite3.sqlite_version)
        return 2
    modules = ['tests.test_database_governance', 'tests.test_database_migrations']
    # Share the selected fixed-behavior regressions with ordinary stability CI.
    from tests.run_stability import DATABASE_REVIEW_FIXES
    modules.extend(DATABASE_REVIEW_FIXES)
    if not args.legacy_only:
        modules.append('tests.test_database_wal')
    return subprocess.run([sys.executable, '-m', 'tests.run_stability', *modules]).returncode


if __name__ == '__main__':
    raise SystemExit(main())
