import os
import threading
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.pool import QueuePool

from app.db.models import Base, CONFIGRSSPARSER
from app.utils import ExceptionUtils, PathUtils
from config import Config

lock = threading.Lock()
_Engine = create_engine(
    f"sqlite:///{os.path.join(Config().get_config_path(), 'user.db')}?check_same_thread=False",
    echo=False,
    poolclass=QueuePool,
    pool_pre_ping=True,
    pool_size=50,
    pool_recycle=60 * 10,
    max_overflow=0
)
_Session = scoped_session(sessionmaker(bind=_Engine,
                                       autoflush=True,
                                       autocommit=False,
                                       expire_on_commit=False))


class MainDb:

    @property
    def session(self):
        return _Session()

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
