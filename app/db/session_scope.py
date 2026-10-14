"""Release SQLite connections at worker-thread boundaries.

A SQLAlchemy 1.4 ``Session`` checks out a pooled connection on its first query
and keeps it until the transaction ends (commit/rollback/close/remove).  Every
read-only helper in this project skips that cleanup, so a *live* thread that
ever queried pins one connection for its whole lifetime.  Background worker
threads live as long as the process, and the pools are finite, so the pool can
be exhausted by idle workers alone.

Rather than adding cleanup to every call site, this module wraps the unit of
work: HTTP requests (Flask teardown), scheduler jobs, thread-pool submissions
and the subtitle worker loops all release their connection when the unit ends.
"""
import functools

from app.db.main_db import remove_session as remove_main_session
from app.db.media_db import remove_session as remove_media_session


def release_db_connections(db=None):
    """
    归还当前线程签出的数据库连接

    :param db: 可选，注入的数据库对象（如测试用的内存库）；优先使用它自身的清理方法
    """
    if db is not None:
        remover = getattr(db, "remove_session", None)
        if callable(remover):
            try:
                remover()
            except Exception:
                pass
    # Injected task stores do not replace the other databases a processor may
    # query. Always clean both global stores, even if one cleanup fails.
    try:
        remove_main_session()
    finally:
        remove_media_session()


def with_db_session(func):
    """
    包装一个工作单元，结束（含异常）时归还连接。保留原函数元信息，使
    apscheduler 依据 __name__/__qualname__ 推导的任务 ID 保持不变。
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        finally:
            release_db_connections()

    return wrapper
