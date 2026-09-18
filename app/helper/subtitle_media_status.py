import os
import time
from datetime import datetime

from sqlalchemy.exc import OperationalError

from app.db.main_db import MainDb
from app.db.models import SUBTITLEMEDIASTATUS


class SubtitleMediaStatusStore:
    """SQLite-backed media subtitle snapshots used by read-only library views."""

    _query_chunk_size = 500

    def __init__(self, db=None):
        self._db = db or MainDb()

    @staticmethod
    def normalize_path(path):
        value = str(path or "").strip()
        if not value:
            return ""
        return os.path.normcase(os.path.abspath(os.path.normpath(value)))

    def list_for_server(self, server):
        server = str(server or "").strip().lower()
        if not server:
            return {}
        try:
            rows = self._db.query(SUBTITLEMEDIASTATUS).filter(
                SUBTITLEMEDIASTATUS.SERVER == server
            ).all()
            return {row.MEDIA_PATH: self._as_dict(row) for row in rows}
        except Exception:
            # Isolated tests and the very first pre-init request may observe a
            # database before create_all has installed the new snapshot table.
            self._db.rollback()
            return {}

    def list_for_paths(self, server, media_paths):
        """Read only requested paths, normalized and chunked below SQLite limits."""
        server = str(server or "").strip().lower()
        paths = []
        seen = set()
        for media_path in media_paths or []:
            normalized = self.normalize_path(media_path)
            if normalized and normalized not in seen:
                seen.add(normalized)
                paths.append(normalized)
        if not server or not paths:
            return {}
        snapshots = {}
        try:
            for offset in range(0, len(paths), self._query_chunk_size):
                chunk = paths[offset:offset + self._query_chunk_size]
                rows = self._db.query(SUBTITLEMEDIASTATUS).filter(
                    SUBTITLEMEDIASTATUS.SERVER == server,
                    SUBTITLEMEDIASTATUS.MEDIA_PATH.in_(chunk)
                ).all()
                snapshots.update({row.MEDIA_PATH: self._as_dict(row) for row in rows})
            return snapshots
        except Exception:
            # Match list_for_server/get behavior during first-run schema setup.
            self._db.rollback()
            return {}

    def get(self, server, media_path):
        key = self.normalize_path(media_path)
        if not key:
            return None
        try:
            row = self._db.query(SUBTITLEMEDIASTATUS).filter(
                SUBTITLEMEDIASTATUS.SERVER == str(server or "").strip().lower(),
                SUBTITLEMEDIASTATUS.MEDIA_PATH == key
            ).first()
            return self._as_dict(row) if row else None
        except Exception:
            self._db.rollback()
            return None

    def upsert_many(self, server, snapshots, source="audit", checked_at=None):
        """Persist only explicitly inspected media; absent paths remain unknown."""
        server = str(server or "").strip().lower()
        now = time.time()
        checked_at = float(checked_at or now)
        saved = 0
        try:
            for snapshot in snapshots or []:
                path = self.normalize_path(snapshot.get("media_path"))
                if not server or not path:
                    continue
                row = self._db.query(SUBTITLEMEDIASTATUS).filter(
                    SUBTITLEMEDIASTATUS.SERVER == server,
                    SUBTITLEMEDIASTATUS.MEDIA_PATH == path
                ).first()
                values = {
                    "MEDIA_EXISTS": self._flag(snapshot.get("media_exists")),
                    "HAS_INTERNAL": self._flag(snapshot.get("has_internal")),
                    "HAS_CHINESE_INTERNAL": self._flag(snapshot.get("has_chinese_internal")),
                    "HAS_EXTERNAL": self._flag(snapshot.get("has_external")),
                    "HAS_CHINESE_EXTERNAL": self._flag(snapshot.get("has_chinese_external")),
                    "STATUS": str(snapshot.get("status") or "unknown"),
                    "SOURCE": str(snapshot.get("source") or source or "unknown"),
                    "CHECKED_AT": checked_at,
                    "UPDATED_AT": now,
                }
                if row:
                    for key, value in values.items():
                        setattr(row, key, value)
                else:
                    self._db.insert(SUBTITLEMEDIASTATUS(
                        SERVER=server, MEDIA_PATH=path, **values
                    ))
                saved += 1
            self._db.commit()
            return saved
        except OperationalError as error:
            self._db.rollback()
            if "no such table" in str(error).lower():
                return 0
            raise
        except Exception:
            self._db.rollback()
            raise

    def delete_paths(self, server, media_paths):
        keys = [self.normalize_path(path) for path in media_paths or []]
        keys = [key for key in keys if key]
        if not keys:
            return 0
        count = self._db.query(SUBTITLEMEDIASTATUS).filter(
            SUBTITLEMEDIASTATUS.SERVER == str(server or "").strip().lower(),
            SUBTITLEMEDIASTATUS.MEDIA_PATH.in_(keys)
        ).delete(synchronize_session=False)
        self._db.commit()
        return count

    @staticmethod
    def _flag(value):
        if value is None:
            return None
        return 1 if bool(value) else 0

    @staticmethod
    def _as_dict(row):
        if not row:
            return None
        return {
            "media_path": row.MEDIA_PATH,
            "media_exists": None if row.MEDIA_EXISTS is None else bool(row.MEDIA_EXISTS),
            "has_internal": None if row.HAS_INTERNAL is None else bool(row.HAS_INTERNAL),
            "has_chinese_internal": None if row.HAS_CHINESE_INTERNAL is None else bool(row.HAS_CHINESE_INTERNAL),
            "has_external": None if row.HAS_EXTERNAL is None else bool(row.HAS_EXTERNAL),
            "has_chinese_external": None if row.HAS_CHINESE_EXTERNAL is None else bool(row.HAS_CHINESE_EXTERNAL),
            "status": row.STATUS or "unknown",
            "source": row.SOURCE or "unknown",
            "checked_at": datetime.fromtimestamp(row.CHECKED_AT).astimezone().isoformat(timespec="seconds"),
            "checked_at_epoch": row.CHECKED_AT,
        }
