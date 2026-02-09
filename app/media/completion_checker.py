# -*- coding: utf-8 -*-
"""
动漫完结状态判断服务
专门处理基于TMDB数据的完结状态判断逻辑
"""
import logging
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Dict, Any, Tuple

from app.utils.types import MediaType

logger = logging.getLogger(__name__)


class CompletionStatus(Enum):
    """完结状态枚举"""
    UNKNOWN = '未知'
    ONGOING = '连载'
    COMPLETED_BY_TMDB = 'TMDB标记已完结'
    COMPLETED_BY_LOCAL = '本地媒体库已完整'
    SUSPICIOUS_COMPLETED = '疑似完结'


class CompletionChecker:
    """完结状态检查服务"""

    def __init__(self):
        self.anime_genre_id = 16  # TMDB中动漫的类型ID
        self.suspicious_days_threshold = 7  # 疑似完结的天数阈值

    def check_completion_status(self,
                              media_info: Dict[str, Any],
                              tmdb_info: Optional[Dict[str, Any]],
                              local_exists: bool = False,
                              local_complete: bool = False) -> Tuple[CompletionStatus, str]:
        """
        综合判断完结状态

        Args:
            media_info: 媒体基础信息
            tmdb_info: TMDB详细信息
            local_exists: 本地是否存在
            local_complete: 本地是否完整

        Returns:
            Tuple[CompletionStatus, str]: (完结状态, 完结原因)
        """
        try:
            # 1. 优先使用TMDB数据判断
            if tmdb_info:
                tmdb_status, tmdb_reason = self._check_tmdb_completion(media_info, tmdb_info)
                if tmdb_status != CompletionStatus.UNKNOWN:
                    logger.info(f"根据TMDB数据判断完结状态: {tmdb_status.value} - {tmdb_reason}")
                    return tmdb_status, tmdb_reason

            # 2. 本地媒体库验证
            if local_exists and local_complete:
                reason = "本地媒体库已包含全部集数"
                logger.info(f"根据本地媒体库判断已完结: {reason}")
                return CompletionStatus.COMPLETED_BY_LOCAL, reason

            # 3. 默认状态
            reason = "无足够信息判断完结状态，继续监控"
            return CompletionStatus.ONGOING, reason

        except Exception as e:
            logger.error(f"完结状态检查出错: {str(e)}")
            return CompletionStatus.UNKNOWN, f"检查过程出错: {str(e)}"

    def _check_tmdb_completion(self,
                             media_info: Dict[str, Any],
                             tmdb_info: Dict[str, Any]) -> Tuple[CompletionStatus, str]:
        """
        基于TMDB数据检查完结状态

        Args:
            media_info: 媒体基础信息
            tmdb_info: TMDB详细信息

        Returns:
            Tuple[CompletionStatus, str]: (完结状态, 完结原因)
        """
        try:
            # 检查TMDB官方完结标记
            status = tmdb_info.get('status', '').strip()
            in_production = tmdb_info.get('in_production', True)

            # 明确标记为已结束且不在制作中
            if status == 'Ended' and not in_production:
                return CompletionStatus.COMPLETED_BY_TMDB, f"TMDB标记为已完结 (status={status}, in_production={in_production})"

            # 检查下一集信息
            next_episode = tmdb_info.get('next_episode_to_air')
            if next_episode is None:
                # 没有下一集信息，可能已完结
                last_air_date = tmdb_info.get('last_air_date')
                if last_air_date and self._is_anime(tmdb_info):
                    # 动漫特殊处理：如果超过阈值天数未更新，认为疑似完结
                    if self._is_long_time_no_update(last_air_date):
                        return CompletionStatus.SUSPICIOUS_COMPLETED, f"动漫超过{self.suspicious_days_threshold}天无下一集信息"

                # 非动漫或动漫但更新时间不长，标记为TMDB完结
                return CompletionStatus.COMPLETED_BY_TMDB, "TMDB显示无下一集播出计划"

            # 有下一集信息，检查播出时间
            next_air_date = next_episode.get('air_date')
            if next_air_date:
                try:
                    next_date = datetime.fromisoformat(next_air_date)
                    if next_date < datetime.now():
                        # 下一集播出时间已过，但可能TMDB数据未及时更新
                        return CompletionStatus.ONGOING, f"下一集计划播出时间已过({next_air_date})，但可能数据未更新"
                except ValueError:
                    logger.warning(f"无法解析下一集播出日期: {next_air_date}")

            # 默认为连载中
            return CompletionStatus.ONGOING, f"TMDB显示连载中 (status={status})"

        except Exception as e:
            logger.error(f"TMDB完结状态检查出错: {str(e)}")
            return CompletionStatus.UNKNOWN, f"TMDB数据解析出错: {str(e)}"

    def _is_anime(self, tmdb_info: Dict[str, Any]) -> bool:
        """
        判断是否为动漫

        Args:
            tmdb_info: TMDB信息

        Returns:
            bool: 是否为动漫
        """
        genre_ids = tmdb_info.get('genre_ids', [])
        genres = tmdb_info.get('genres', [])

        # 检查genre_ids
        if self.anime_genre_id in genre_ids:
            return True

        # 检查genres数组
        for genre in genres:
            if isinstance(genre, dict) and genre.get('id') == self.anime_genre_id:
                return True

        return False

    def _is_long_time_no_update(self, last_air_date: str) -> bool:
        """
        检查是否超过阈值天数未更新

        Args:
            last_air_date: 最后播出日期字符串

        Returns:
            bool: 是否超过阈值天数
        """
        try:
            last_date = datetime.fromisoformat(last_air_date)
            days_since_last = (datetime.now() - last_date).days
            return days_since_last > self.suspicious_days_threshold
        except (ValueError, TypeError):
            logger.warning(f"无法解析最后播出日期: {last_air_date}")
            return False

    def get_completion_reason(self, completion_status: CompletionStatus) -> str:
        """
        获取完结原因说明

        Args:
            completion_status: 完结状态

        Returns:
            str: 完结原因说明
        """
        reason_map = {
            CompletionStatus.UNKNOWN: "无法确定完结状态",
            CompletionStatus.ONGOING: "正在连载中",
            CompletionStatus.COMPLETED_BY_TMDB: "根据TMDB官方数据判定为已完结",
            CompletionStatus.COMPLETED_BY_LOCAL: "根据本地媒体库判定为已完结",
            CompletionStatus.SUSPICIOUS_COMPLETED: "疑似已完结，但需进一步确认"
        }
        return reason_map.get(completion_status, "未知状态")

    def is_anime_completed_by_tmdb(self, tmdb_info: Dict[str, Any]) -> bool:
        """
        基于TMDB判断动漫是否完结（兼容方法）

        Args:
            tmdb_info: TMDB信息

        Returns:
            bool: 是否完结
        """
        if not self._is_anime(tmdb_info):
            return False

        status, _ = self._check_tmdb_completion({}, tmdb_info)
        return status in [CompletionStatus.COMPLETED_BY_TMDB, CompletionStatus.SUSPICIOUS_COMPLETED]

    def get_next_episode_date(self, tmdb_info: Dict[str, Any]) -> Optional[datetime]:
        """
        获取下一集播出时间

        Args:
            tmdb_info: TMDB信息

        Returns:
            Optional[datetime]: 下一集播出时间，如果没有则返回None
        """
        next_ep = tmdb_info.get('next_episode_to_air')
        if next_ep and next_ep.get('air_date'):
            try:
                return datetime.fromisoformat(next_ep['air_date'])
            except ValueError:
                logger.warning(f"无法解析下一集播出日期: {next_ep['air_date']}")
        return None

    def is_long_time_no_update(self, tmdb_info: Dict[str, Any], days: int = 7) -> bool:
        """
        检查是否超过指定天数未更新

        Args:
            tmdb_info: TMDB信息
            days: 天数阈值

        Returns:
            bool: 是否超过指定天数
        """
        last_date = tmdb_info.get('last_air_date')
        if last_date:
            try:
                last = datetime.fromisoformat(last_date)
                return (datetime.now() - last).days > days
            except ValueError:
                logger.warning(f"无法解析最后播出日期: {last_date}")
        return False