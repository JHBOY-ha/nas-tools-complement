"""Validated SQLite budgets; reading settings never rewrites user configuration."""
import logging
from dataclasses import dataclass


@dataclass(frozen=True)
class DatabaseSettings:
    journal_mode: str = 'auto'
    busy_timeout_seconds: int = 30
    writer_queue_size: int = 64
    writer_wait_seconds: int = 30
    audit_batch_rows: int = 1000
    wal_autocheckpoint_pages: int = 1000
    wal_warning_mb: int = 64
    wal_limit_mb: int = 256
    # Small /config volumes retain a safety margin without requiring 2 GiB free.
    # Explicit administrator overrides remain authoritative.
    reserve_free_mb: int = 256

    @classmethod
    def from_config(cls, config=None):
        if config is None:
            from config import Config
            config = Config().get_config('app')
        raw = (config or {}).get('database') or {}
        if not isinstance(raw, dict):
            raise ValueError('app.database 必须是配置对象')
        mode = raw.get('journal_mode', 'auto')
        if mode not in ('auto', 'wal', 'delete'):
            raise ValueError('app.database.journal_mode 必须是 auto/wal/delete')
        defaults = cls()
        limits = {
            'busy_timeout_seconds': (1, 120), 'writer_queue_size': (1, 256),
            'writer_wait_seconds': (1, 120), 'audit_batch_rows': (100, 1000),
            'wal_autocheckpoint_pages': (100, 16000), 'wal_warning_mb': (4, 4096),
            'wal_limit_mb': (8, 8192), 'reserve_free_mb': (256, 51200),
        }
        values = {'journal_mode': mode}
        for name, (minimum, maximum) in limits.items():
            try:
                value = raw.get(name, getattr(defaults, name))
                if isinstance(value, bool):
                    raise ValueError()
                value = int(str(value))
                if not minimum <= value <= maximum:
                    raise ValueError()
            except (ValueError, TypeError):
                value = getattr(defaults, name)
                logging.getLogger(__name__).warning('数据库预算 %s 无效，使用默认值 %s', name, value)
            values[name] = value
        if values['wal_warning_mb'] >= values['wal_limit_mb']:
            raise ValueError('数据库 WAL 高水位必须大于警戒值')
        return cls(**values)


def wal_runtime_supported(version):
    """Accept fixed branches, not every version newer than the oldest backport."""
    value = tuple(int(part) for part in version)
    return (value >= (3, 51, 3)
            or value[:2] == (3, 50) and value >= (3, 50, 7)
            or value[:2] == (3, 44) and value >= (3, 44, 6))
