"""Offline backup administration. Stop the service before invoking this module.

python3 -m scripts.database_maintenance --config-dir /config list
python3 -m scripts.database_maintenance --config-dir /config cancel-restore
python3 -m scripts.database_maintenance --config-dir /config delete-backup NAME.zip
"""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-dir', required=True, type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('list', help='List local ZIPs, upgrade snapshots and restore state')
    commands.add_parser('cancel-restore', help='Cancel a restore that has not begun applying')
    commands.add_parser('delete-backup', help='Permanently delete the named generated ZIP').add_argument('name')
    commands.add_parser('delete-incomplete-upgrade', help='Delete only the named unfinished copy').add_argument('name')
    args = parser.parse_args()
    directory = args.config_dir.resolve()
    config = directory / 'config.yaml'
    if not directory.is_dir() or not config.is_file() or config.is_symlink():
        parser.error('--config-dir must contain an existing regular config.yaml')
    # The project constructs engine URLs on import. Set the explicit directory
    # first; never inherit an unrelated live/default configuration by accident.
    os.environ['NASTOOL_CONFIG'] = str(config)
    from app.db.runtime import acquire_instance
    from app.db.backup import cancel_pending_restore, delete_backup_archive, delete_incomplete_upgrade
    from app.db.transactions import DatabaseBusy, DatabaseWriteError
    try:
        # The same process lease as startup excludes a running app and other
        # maintenance commands. No database preparation/migration is performed.
        acquire_instance(directory)
        if args.command == 'cancel-restore':
            print(json.dumps(cancel_pending_restore(directory), ensure_ascii=False))
        elif args.command == 'delete-backup':
            delete_backup_archive(directory, args.name)
            print('已删除备份：' + args.name)
        elif args.command == 'delete-incomplete-upgrade':
            delete_incomplete_upgrade(directory, args.name)
            print('已删除未完成副本：' + args.name)
        else:
            result = {'pending_restore': (directory / '.db-restore-pending.json').exists()}
            for folder in ('backup_file', '.db-upgrades', '.db-restores'):
                root = directory / folder
                if root.is_symlink():
                    raise DatabaseWriteError('备份目录不能是软链接')
                result[folder] = sorted(path.name for path in root.iterdir()) if root.exists() else []
            print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (DatabaseBusy, DatabaseWriteError, OSError, ValueError) as error:
        # These operational failures must not start services or silently remove
        # another snapshot. Preserve the files for the operator to investigate.
        print('数据库维护未完成：' + str(error))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
