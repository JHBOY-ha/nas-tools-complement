import os
import threading
import time
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.pool import QueuePool

import log
from app.db.models import Base, CONFIGRSSPARSER
from app.utils import ExceptionUtils, PathUtils
from config import Config
from .settings import DatabaseSettings
from .transactions import ManagedDatabase, DatabaseBusy, DatabaseWriteError

lock = threading.Lock()
# pysqlite 的 timeout 就是 SQLite 的 busy timeout：默认 5 秒，在群晖慢盘或
# 并发写入下极易抛 "database is locked"。提升到 30 秒让写者排队而不是直接失败。
_SQLITE_BUSY_TIMEOUT_SECONDS = 30
_POOL_WARN_RATIO = 0.8
_pool_warn_at = [0.0]

_Engine = create_engine(
    f"sqlite:///{os.path.join(Config().get_config_path(), 'user.db')}?check_same_thread=False",
    echo=False,
    hide_parameters=True,
    poolclass=QueuePool,
    pool_pre_ping=True,
    # 容量与原先的 pool_size=50/max_overflow=0 保持一致，避免在突发并发下
    # 反而更容易触发 QueuePool 超时；真正的连接泄漏由工作单元结束时的
    # remove_session() 归还连接来消除。LIFO 让空闲连接更快被复用回收。
    pool_size=20,
    max_overflow=30,
    pool_timeout=30,
    pool_use_lifo=True,
    pool_recycle=60 * 10,
    connect_args={"timeout": _SQLITE_BUSY_TIMEOUT_SECONDS}
)

# Read connections cannot autoflush a mutation. One reusable write connection
# per database is admitted by the same process-wide FIFO transaction budget.
_Settings = DatabaseSettings.from_config()
_WriteEngine = create_engine(_Engine.url, poolclass=QueuePool, pool_size=1,
                             max_overflow=0, pool_pre_ping=True, hide_parameters=True,
                             connect_args={'timeout': _Settings.busy_timeout_seconds})
_Database = ManagedDatabase(_Engine, _WriteEngine, _Settings,
                            path=os.path.join(Config().get_config_path(), 'user.db'))


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
    log.warn("【Db】user.db 连接池接近耗尽：%s/%s，可能存在未归还的会话" % (checked_out, capacity))


_Session = _Database._reads


def remove_session():
    """
    结束当前线程的会话并归还其签出的连接

    只读查询同样会签出连接并持有到事务结束，因此常驻线程必须在其工作单元
    结束时调用本函数，否则每个存活线程会永久占用一条连接。
    """
    _Database.remove_session()


class MainDb:

    @property
    def session(self):
        return _Database.session

    def write_transaction(self, required_bytes=0):
        return _Database.write_transaction(required_bytes=required_bytes)

    def read_snapshot(self):
        return _Database.read_snapshot()

    def reserve_write(self):
        return _Database.reserve_write()

    @staticmethod
    def remove_session():
        remove_session()

    @staticmethod
    def init_db():
        with lock:
            from .publication import seed_legacy
            with _Database.maintenance() as connection:
                with connection.begin():
                    connection.exec_driver_sql('BEGIN IMMEDIATE')
                    Base.metadata.create_all(connection)
                    seed_legacy(connection)

    def init_data(self):
        """
        读取config目录下的sql文件，并初始化到数据库，只处理一次
        """
        config = Config().get_config()
        init_files = list(Config().get_config("app").get("init_files") or [])
        config_dir = os.path.join(Config().get_root_path(), "config")
        sql_files = PathUtils.get_dir_level1_files(in_path=config_dir, exts=".sql")
        config_flag = False
        for sql_file in sql_files:
            filename = os.path.basename(sql_file)
            if self.__is_init_file_complete(filename, init_files):
                continue

            config_flag = True
            try:
                with open(sql_file, "r", encoding="utf-8") as f:
                    sql_list = [sql.strip() for sql in f.read().split(';\n') if sql.strip()]
                with self.write_transaction():
                    for sql in sql_list:
                        self.excute(sql)
            except Exception as err:
                self.rollback()
                print("初始化 SQL 文件 %s 失败：%s" % (filename, str(err)))
                continue

            if filename not in init_files:
                init_files.append(filename)
        if config_flag:
            config['app']['init_files'] = init_files
            Config().save_config(config)

    def __is_init_file_complete(self, filename, init_files):
        """
        判断初始化脚本是否确实完成，避免仅依赖配置文件中的记录。
        """
        if filename not in init_files:
            return False
        if filename != "init_userrss_v3.sql":
            return True

        required_parser_ids = {1, 2, 3, 4, 5}
        parser_ids = {
            row[0] for row in self.query(CONFIGRSSPARSER.ID)
            .filter(CONFIGRSSPARSER.ID.in_(required_parser_ids))
            .all()
        }
        return parser_ids == required_parser_ids

    def insert(self, data):
        """
        插入数据
        """
        if isinstance(data, list):
            self.session.add_all(data)
        else:
            self.session.add(data)

    def query(self, *obj):
        """
        查询对象
        """
        from .publication import filter_visible
        return filter_visible(self.session.query(*obj), obj)

    def excute(self, sql):
        """
        执行SQL语句
        """
        self.session.execute(sql)

    def flush(self):
        """
        刷写
        """
        self.session.flush()

    def commit(self):
        """
        提交事务
        """
        _Database.commit()

    def rollback(self):
        """
        回滚事务
        """
        _Database.rollback()


class DbPersist(object):
    """
    数据库持久化装饰器。

    返回值契约：``None`` 表示正常执行结束，对外统一返回 ``True``；明确的
    ``False`` 表示本次操作被拒绝或失败，并撤销当前工作单元。嵌套调用中的
    ``False`` 会把整个外层写事务标记为只能回滚，即使调用方忽略返回值或继续
    执行其他方法，外层也不能提交。合法的重复操作、数据已经符合要求的无变化
    操作应返回 ``True``（或保持 ``None`` 让装饰器转换为 ``True``）。除
    ``False`` 外，``0``、空列表等由具体方法定义的返回值不会被装饰器解释为失败。
    """

    def __init__(self, db):
        self.db = db

    def __call__(self, f):
        def persist(*args, **kwargs):
            # Resolve injected stores from the actual receiver, rather than
            # committing a captured global store while a fixture writes another.
            receiver = args[0] if args else None
            db = getattr(receiver, '_db', receiver if hasattr(receiver, 'write_transaction') else self.db)
            from .transactions import write_transaction
            try:
                with write_transaction(db):
                    ret = f(*args, **kwargs)
                    if ret is False:
                        # Use identity, not truthiness: 0 and empty containers
                        # are method-level results, while False is the only
                        # return value that requests a transactional rollback.
                        raise _PersistRejected()
                return True if ret is None else ret
            except _PersistRejected:
                # The surrounding write_transaction already rolled back this
                # unit; for a nested call it also left the outer unit marked
                # rollback-only before this exception was converted to False.
                return False
            except (DatabaseBusy, DatabaseWriteError):
                # Admission rejection/uncertain commit must reach the existing
                # Web/task error envelope, never become an ignored False.
                raise
            except Exception as e:
                ExceptionUtils.exception_traceback(e)
                return False

        return persist


class _PersistRejected(Exception):
    """Preserve explicit False while rolling back its enclosing write unit."""
