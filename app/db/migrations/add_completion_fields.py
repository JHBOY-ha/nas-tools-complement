# -*- coding: utf-8 -*-
"""
数据库迁移: 为RSSTV表添加完结状态相关字段
Migration: Add completion status fields to RSSTV table
"""
import logging
from sqlalchemy import text

logger = logging.getLogger(__name__)


def upgrade(db):
    """
    添加完结状态相关字段到RSS_TVS表

    Args:
        db: 数据库连接对象
    """
    try:
        logger.info("开始执行数据库迁移: 添加完结状态字段")

        # 添加完结状态字段
        migration_sql = [
            "ALTER TABLE RSS_TVS ADD COLUMN COMPLETION_STATUS TEXT DEFAULT 'UNKNOWN'",
            "ALTER TABLE RSS_TVS ADD COLUMN TMDB_STATUS TEXT",
            "ALTER TABLE RSS_TVS ADD COLUMN LAST_COMPLETION_CHECK DATETIME",
            "ALTER TABLE RSS_TVS ADD COLUMN COMPLETION_REASON TEXT"
        ]

        for sql in migration_sql:
            try:
                db.execute(text(sql))
                logger.info(f"执行SQL成功: {sql}")
            except Exception as e:
                # 如果字段已存在，忽略错误
                if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                    logger.warning(f"字段可能已存在，跳过: {sql} - {e}")
                    continue
                else:
                    raise e

        # 为现有数据设置默认值
        update_sql = """
        UPDATE RSS_TVS
        SET COMPLETION_STATUS = 'UNKNOWN',
            COMPLETION_REASON = '历史数据，状态未知'
        WHERE COMPLETION_STATUS IS NULL
        """

        try:
            result = db.execute(text(update_sql))
            logger.info(f"更新现有数据默认值: 影响 {result.rowcount} 行")
        except Exception as e:
            logger.warning(f"更新默认值失败，但不影响迁移: {e}")

        db.commit()
        logger.info("数据库迁移完成: 添加完结状态字段成功")
        return True

    except Exception as e:
        logger.error(f"数据库迁移失败: {e}")
        db.rollback()
        raise e


def downgrade(db):
    """
    回滚迁移: 删除完结状态相关字段

    Args:
        db: 数据库连接对象
    """
    try:
        logger.info("开始回滚数据库迁移: 删除完结状态字段")

        # SQLite 不支持直接删除列，所以需要重建表
        # 这里只记录日志，实际回滚需要更复杂的操作
        logger.warning("SQLite不支持DROP COLUMN，回滚需要手动处理")
        logger.info("如需完全回滚，请备份数据并重建表结构")

        return True

    except Exception as e:
        logger.error(f"数据库迁移回滚失败: {e}")
        raise e


def check_migration_needed(db):
    """
    检查是否需要执行迁移

    Args:
        db: 数据库连接对象

    Returns:
        bool: 是否需要迁移
    """
    try:
        # 检查COMPLETION_STATUS字段是否存在
        result = db.execute(text("PRAGMA table_info(RSS_TVS)"))
        columns = [row[1] for row in result.fetchall()]

        needed_columns = ['COMPLETION_STATUS', 'TMDB_STATUS', 'LAST_COMPLETION_CHECK', 'COMPLETION_REASON']
        missing_columns = [col for col in needed_columns if col not in columns]

        if missing_columns:
            logger.info(f"需要迁移，缺少字段: {missing_columns}")
            return True
        else:
            logger.info("所有必要字段都已存在，无需迁移")
            return False

    except Exception as e:
        logger.error(f"检查迁移状态失败: {e}")
        return True  # 出错时保守地选择执行迁移


def execute_migration(db):
    """
    执行迁移的主函数

    Args:
        db: 数据库连接对象

    Returns:
        bool: 迁移是否成功
    """
    try:
        if check_migration_needed(db):
            return upgrade(db)
        else:
            logger.info("跳过迁移，字段已存在")
            return True

    except Exception as e:
        logger.error(f"执行迁移出错: {e}")
        return False


if __name__ == "__main__":
    # 测试用代码，实际使用时应该通过数据库初始化流程调用
    print("这是一个数据库迁移脚本，应该通过数据库初始化流程调用")
    print("Migration script should be called through database initialization process")