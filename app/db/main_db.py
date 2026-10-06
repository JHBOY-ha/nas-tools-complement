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

lock = threading.Lock()
# pysqlite 的 timeout 就是 SQLite 的 busy timeout：默认 5 秒，在群晖慢盘或
# 并发写入下极易抛 "database is locked"。提升到 30 秒让写者排队而不是直接失败。
_SQLITE_BUSY_TIMEOUT_SECONDS = 30
_POOL_WARN_RATIO = 0.8
_pool_warn_at = [0.0]

_Engine = create_engine(
    f"sqlite:///{os.path.join(Config().get_config_path(), 'user.db')}?check_same_thread=False",
    echo=False,
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


_Session = scoped_session(sessionmaker(bind=_Engine,
                                       autoflush=True,
                                       autocommit=False,
                                       expire_on_commit=False))


def remove_session():
    """
    结束当前线程的会话并归还其签出的连接

    只读查询同样会签出连接并持有到事务结束，因此常驻线程必须在其工作单元
    结束时调用本函数，否则每个存活线程会永久占用一条连接。
    """
    _Session.remove()


class MainDb:

    @property
    def session(self):
        return _Session()

    @staticmethod
    def remove_session():
        remove_session()

    @staticmethod
    def init_db():
        with lock:
            Base.metadata.create_all(_Engine)

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
                for sql in sql_list:
                    self.excute(sql)
                self.commit()
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
        return self.session.query(*obj)

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
        self.session.commit()

    def rollback(self):
        """
        回滚事务
        """
        self.session.rollback()


class DbPersist(object):
    """
    数据库持久化装饰器
    """

    def __init__(self, db):
        self.db = db

    def __call__(self, f):
        def persist(*args, **kwargs):
            try:
                ret = f(*args, **kwargs)
                self.db.commit()
                return True if ret is None else ret
            except Exception as e:
                ExceptionUtils.exception_traceback(e)
                self.db.rollback()
                return False

        return persist
