import os
import json
import threading
import time
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.pool import QueuePool
import log
from app.db.models import BaseMedia, MEDIASYNCITEMS, MEDIASYNCSTATISTIC
from app.utils import ExceptionUtils
from config import Config

lock = threading.Lock()
# 与 user.db 一致：默认 5 秒 busy timeout 在慢盘并发写时太短。
_SQLITE_BUSY_TIMEOUT_SECONDS = 30
_POOL_WARN_RATIO = 0.8
_pool_warn_at = [0.0]

_Engine = create_engine(
    f"sqlite:///{os.path.join(Config().get_config_path(), 'media.db')}?check_same_thread=False",
    echo=False,
    poolclass=QueuePool,
    pool_pre_ping=True,
    # 与 user.db 一致：容量与原先持平，泄漏改在工作单元结束时归还连接。
    pool_size=20,
    max_overflow=30,
    pool_timeout=30,
    pool_use_lifo=True,
    pool_recycle=60 * 10,
    connect_args={"timeout": _SQLITE_BUSY_TIMEOUT_SECONDS}
)


@event.listens_for(_Engine, "checkout")
def _warn_on_pool_pressure(dbapi_connection, connection_record, connection_proxy):
    """连接池接近耗尽时告警，用于量化会话泄漏（限量日志，避免刷屏）。"""
    try:
        capacity = _Engine.pool.size() + _Engine.pool._max_overflow
        checked_out = _Engine.pool.checkedout()
    except Exception:
        return
    if not capacity or checked_out < capacity * _POOL_WARN_RATIO:
        return
    now = time.monotonic()
    if now - _pool_warn_at[0] < 60:
        return
    _pool_warn_at[0] = now
    log.warn("【Db】media.db 连接池接近耗尽：%s/%s，可能存在未归还的会话" % (checked_out, capacity))


_Session = scoped_session(sessionmaker(bind=_Engine,
                                       autoflush=True,
                                       autocommit=False,
                                       # 与 user.db 对齐：commit 后对象不再过期，
                                       # 避免属性访问触发 refresh 再次签出连接。
                                       expire_on_commit=False))


def remove_session():
    """
    结束当前线程的会话并归还其签出的连接（语义同 user.db）
    """
    _Session.remove()


class MediaDb:

    @property
    def session(self):
        return _Session()

    @staticmethod
    def remove_session():
        remove_session()

    @staticmethod
    def init_db():
        with lock:
            BaseMedia.metadata.create_all(_Engine)

    def insert(self, server_type, iteminfo):
        if not server_type or not iteminfo:
            return False
        try:
            self.session.query(MEDIASYNCITEMS).filter(MEDIASYNCITEMS.SERVER == server_type,
                                                      MEDIASYNCITEMS.ITEM_ID == iteminfo.get("id")).delete()
            self.session.flush()
            self.session.add(MEDIASYNCITEMS(
                SERVER=server_type,
                LIBRARY=iteminfo.get("library"),
                ITEM_ID=iteminfo.get("id"),
                ITEM_TYPE=iteminfo.get("type"),
                TITLE=iteminfo.get("title"),
                ORGIN_TITLE=iteminfo.get("originalTitle"),
                YEAR=iteminfo.get("year"),
                TMDBID=iteminfo.get("tmdbid"),
                IMDBID=iteminfo.get("imdbid"),
                PATH=iteminfo.get("path"),
                JSON=iteminfo.get("json") if iteminfo.get("json") else json.dumps(iteminfo, ensure_ascii=False)
            ))
            self.session.commit()
            return True
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            self.session.rollback()
        return False

    def empty(self, server_type=None, library=None):
        try:
            if server_type and library:
                self.session.query(MEDIASYNCITEMS).filter(MEDIASYNCITEMS.SERVER == server_type,
                                                          MEDIASYNCITEMS.LIBRARY == library).delete()
            else:
                self.session.query(MEDIASYNCITEMS).delete()
            self.session.commit()
            return True
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            self.session.rollback()
        return False

    def statistics(self, server_type, total_count, movie_count, tv_count):
        if not server_type:
            return False
        try:
            self.session.query(MEDIASYNCSTATISTIC).filter(MEDIASYNCSTATISTIC.SERVER == server_type).delete()
            self.session.flush()
            self.session.add(MEDIASYNCSTATISTIC(
                SERVER=server_type,
                TOTAL_COUNT=total_count,
                MOVIE_COUNT=movie_count,
                TV_COUNT=tv_count,
                UPDATE_TIME=time.strftime('%Y-%m-%d %H:%M:%S',
                                          time.localtime(time.time()))
            ))
            self.session.commit()
            return True
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            self.session.rollback()
        return False

    def exists(self, server_type, title, year, tmdbid):
        if not server_type or not title:
            return False
        if tmdbid:
            count = self.session.query(MEDIASYNCITEMS).filter(MEDIASYNCITEMS.TMDBID == str(tmdbid)).count()
            if count:
                return True
        if year:
            items = self.session.query(MEDIASYNCITEMS).filter(MEDIASYNCITEMS.SERVER == server_type,
                                                              MEDIASYNCITEMS.TITLE == title,
                                                              MEDIASYNCITEMS.YEAR == str(year)).all()
        else:
            items = self.session.query(MEDIASYNCITEMS).filter(MEDIASYNCITEMS.SERVER == server_type,
                                                              MEDIASYNCITEMS.TITLE == title).all()
        if items:
            if tmdbid:
                for item in items:
                    if not item.TMDBID or item.TMDBID == str(tmdbid):
                        return True
                return False
            else:
                return True
        else:
            return False

    def get_statistics(self, server_type):
        if not server_type:
            return None
        return self.session.query(MEDIASYNCSTATISTIC).filter(MEDIASYNCSTATISTIC.SERVER == server_type).first()

    def list_items(self, server_type=None):
        """
        查询媒体库同步项目
        """
        try:
            query = self.session.query(MEDIASYNCITEMS)
            if server_type:
                server_names = {server_type, str(server_type).lower(), str(server_type).capitalize()}
                query = query.filter(MEDIASYNCITEMS.SERVER.in_(list(server_names)))
            return query.order_by(MEDIASYNCITEMS.TITLE.asc()).all()
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return []

    def find_items(self, server_type=None, item_ids=None, library=None, path=None):
        """Narrow media-sync lookup used by targeted subtitle refresh validation."""
        try:
            query = self.session.query(MEDIASYNCITEMS)
            if server_type:
                names = {str(server_type), str(server_type).lower(), str(server_type).capitalize()}
                query = query.filter(MEDIASYNCITEMS.SERVER.in_(list(names)))
            ids = [str(value) for value in (item_ids or []) if str(value or "").strip()]
            if ids:
                query = query.filter(MEDIASYNCITEMS.ITEM_ID.in_(ids))
            if library:
                query = query.filter(MEDIASYNCITEMS.LIBRARY == str(library))
            if path:
                query = query.filter(MEDIASYNCITEMS.PATH == os.path.normpath(str(path)))
            return query.order_by(MEDIASYNCITEMS.TITLE.asc()).all()
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return []
