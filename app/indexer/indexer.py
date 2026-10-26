import datetime
import time
from concurrent.futures import wait, FIRST_COMPLETED

import log
from app.conf import ModuleConf
from app.db.session_scope import with_db_session
from app.helper import ProgressHelper, SubmoduleHelper
from app.indexer.client import BuiltinIndexer
from app.utils import ExceptionUtils, StringUtils
from app.utils.commons import singleton
from app.utils.types import SearchType, IndexerType
from app.utils.workload import BoundedExecutor, TaskQueueFull
from config import Config

# 单次并行检索的整体时限。单个站点卡住不应拖垮整次检索；各站点请求自身仍有
# 独立超时，这里的上限只是兜底。渲染类站点最坏约 55 秒，故取值高于它。
SEARCH_TOTAL_TIMEOUT_SECONDS = 120


@singleton
class Indexer(object):
    _indexer_schemas = []
    _client = None
    _client_type = None
    progress = None

    def __init__(self):
        # All searches share this budget, including calls whose HTTP caller has
        # already timed out. Slow sites cannot create another pool on each click.
        self._search_executor = BoundedExecutor(
            Config().get_workload_limit('search_workers'),
            Config().get_workload_limit('search_queue_size'),
            'nastool-search'
        )
        self._indexer_schemas = SubmoduleHelper.import_submodules(
            'app.indexer.client',
            filter_func=lambda _, obj: hasattr(obj, 'schema')
        )
        log.debug(f"【Indexer】: 已经加载的索引器：{self._indexer_schemas}")
        self.init_config()

    def init_config(self):
        self.progress = ProgressHelper()
        self._client_type = ModuleConf.INDEXER_DICT.get(
            Config().get_config("pt").get('search_indexer') or 'builtin'
        )
        self._client = self.__get_client(self._client_type)

    def __build_class(self, ctype, conf):
        for indexer_schema in self._indexer_schemas:
            try:
                if indexer_schema.match(ctype):
                    return indexer_schema(conf)
            except Exception as e:
                ExceptionUtils.exception_traceback(e)
        return None

    def get_indexers(self):
        """
        获取当前索引器的索引站点
        """
        if not self._client:
            return []
        return self._client.get_indexers()

    def get_indexer_dict(self):
        """
        获取索引器字典
        """
        return [
            {
                "id": index.id,
                "name": index.name
            } for index in self.get_indexers()
        ]

    def get_indexer_hash_dict(self):
        """
        获取索引器Hash字典
        """
        IndexerDict = {}
        for item in self.get_indexers() or []:
            IndexerDict[StringUtils.md5_hash(item.name)] = {
                "id": item.id,
                "name": item.name,
                "public": item.public,
                "builtin": item.builtin
            }
        return IndexerDict

    def get_indexer_names(self):
        """
        获取当前索引器的索引站点名称
        """
        return [indexer.name for indexer in self.get_indexers()]

    @staticmethod
    def get_builtin_indexers(check=True, public=True, indexer_id=None):
        """
        获取内置索引器的索引站点
        """
        return BuiltinIndexer().get_indexers(check=check, public=public, indexer_id=indexer_id)

    @staticmethod
    def list_builtin_resources(index_id, page=0, keyword=None):
        """
        获取内置索引器的资源列表
        :param index_id: 内置站点ID
        :param page: 页码
        :param keyword: 搜索关键字
        """
        return BuiltinIndexer().list(index_id=index_id, page=page, keyword=keyword)

    def __get_client(self, ctype: IndexerType, conf=None):
        return self.__build_class(ctype=ctype.value, conf=conf)

    def get_client(self):
        """
        获取当前索引器
        """
        return self._client

    def get_client_type(self):
        """
        获取当前索引器类型
        """
        return self._client_type

    def search_by_keyword(self,
                          key_word: [str, list],
                          filter_args: dict,
                          match_media=None,
                          in_from: SearchType = None,
                          search_context: dict = None):
        """
        根据关键字调用 Index API 检索
        :param key_word: 检索的关键字，不能为空
        :param filter_args: 过滤条件，对应属性为空则不过滤，{"season":季, "episode":集, "year":年, "type":类型, "site":站点,
                            "":, "restype":质量, "pix":分辨率, "sp_state":促销状态, "key":其它关键字}
                            sp_state: 为UL DL，* 代表不关心，
        :param match_media: 需要匹配的媒体信息
        :param in_from: 搜索渠道
        :param search_context: 同一次检索两轮共用的片名和已提交站点查询，不能跨请求复用
        :return: 命中的资源媒体信息列表
        """
        if not key_word:
            return []

        indexers = self.get_indexers()
        if not indexers:
            log.error(f"【{self._client_type.value}】没有有效的索引器配置！")
            return []
        # 按站点语言选择片名，不能等所有中文站都无结果才补搜英文站。
        # 分发前只解析一次，避免多个站点并发查询同一影片的英文详情。
        # 自定义关键字和未识别的快速搜索保留用户输入，不擅自替换。
        if search_context is None:
            search_context = {}
        submitted = search_context.setdefault("submitted", set())
        report = search_context.setdefault("report", {})
        observer = search_context.get("on_progress")
        cancel = search_context.get("cancel_event")
        if observer:
            filter_args = dict(filter_args or {}, _session_search=True)
        english_word = None
        if (self._client_type == IndexerType.BUILTIN
                and match_media and not match_media.keyword
                and StringUtils.is_chinese(key_word)
                and any(index.language == "en" and
                        (not filter_args or not filter_args.get("site")
                         or index.name in filter_args["site"]) for index in indexers)):
            candidates = [search_context.get("english_word"), match_media.en_name]
            if match_media.original_language == "en":
                candidates.append(match_media.original_title)
            title = next((name for name in candidates
                          if name and name.strip() and not StringUtils.is_chinese(name)), None)
            if not title and "english_word" not in search_context and match_media.tmdb_id:
                try:
                    # 英文查询使用请求级语言，可安全复用现有媒体客户端。
                    title = self._client.media.get_tmdb_en_title(match_media)
                except Exception as err:
                    ExceptionUtils.exception_traceback(err)
            search_context["english_word"] = title
            if title and title.strip() and not StringUtils.is_chinese(title):
                english_word = title
            else:
                english_word = None
        # 计算耗时
        start_time = datetime.datetime.now()
        if filter_args and filter_args.get("site"):
            log.info(f"【{self._client_type.value}】开始检索 %s，站点：%s ..." % (key_word, filter_args.get("site")))
            if not observer:
                self.progress.update(ptype='search', text="开始检索 %s，站点：%s ..." % (key_word, filter_args.get("site")))
        else:
            concurrency = self._search_executor.snapshot()['max_workers']
            log.info(f"【{self._client_type.value}】开始并行检索 %s，站点数：%s，共享并发上限：%s ..."
                     % (key_word, len(indexers), concurrency))
            if not observer:
                self.progress.update(ptype='search', text="开始检索 %s，站点数：%s，并发上限：%s ..."
                                     % (key_word, len(indexers), concurrency))
        # Network searches use a separate, process-wide FIFO queue so a waiting
        # background task cannot consume its own executor's remaining workers.
        executor = self._search_executor
        all_task = []
        task_queries = {}
        task_sites = {}
        ret_array = []
        unfinished = 0
        try:
            for index in indexers:
                if cancel and cancel.is_set():
                    break
                if filter_args and filter_args.get("site") and index.name not in filter_args["site"]:
                    continue
                order_seq = 100 - int(index.pri)
                site_word = key_word
                if english_word and index.language == "en":
                    site_word = english_word
                    log.info(f"【{self._client_type.value}】{index.name} 使用英文名 {site_word} 检索")
                # 两轮搜索按实际发送的关键词去重；记录仅属于本次用户请求。
                normalized_word = StringUtils.handler_special_chars(
                    text=site_word, replace_word=" ", allow_space=True)
                query_key = (self._client_type.value, index.id,
                             tuple(normalized_word) if isinstance(normalized_word, list) else normalized_word)
                if query_key in submitted:
                    continue
                # 池内线程由 executor 复用，工作单元结束时归还数据库连接。
                stats = {"name": getattr(index, 'name', str(index.id)), "state": "searching"}
                report[str(index.id)] = stats
                try:
                    task = executor.submit(with_db_session(self._client.search),
                                           order_seq,
                                           index,
                                           site_word,
                                           filter_args,
                                           match_media,
                                           in_from,
                                           **({'stats': stats} if self._client_type == IndexerType.BUILTIN else {}))
                except TaskQueueFull:
                    stats["state"] = "busy"
                    unfinished += 1
                    continue
                # 未准入的任务不能记为已搜索，下一轮仍允许尝试提交。
                submitted.add(query_key)
                all_task.append(task)
                task_queries[task] = query_key
                task_sites[task] = stats
            if unfinished:
                log.warn("【%s】检索队列繁忙，%s 个站点未准入，请稍后重试"
                         % (self._client_type.value, unfinished))
            pending = set(all_task)
            deadline = time.monotonic() + SEARCH_TOTAL_TIMEOUT_SECONDS
            if observer:
                observer([], report)
            # Short waits allow cancellation without interrupting an in-flight
            # HTTP call; its worker slot remains held until the real call ends.
            while pending and not (cancel and cancel.is_set()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                done, pending = wait(pending, timeout=min(0.25, remaining), return_when=FIRST_COMPLETED)
                for future in done:
                    stats = task_sites[future]
                    try:
                        result = future.result() or []
                        stats.update(valid=len(result))
                        if stats['state'] == 'searching':
                            stats['state'] = 'done' if result else 'empty'
                    except Exception as err:
                        ExceptionUtils.exception_traceback(err)
                        stats['state'] = 'failed'
                        unfinished += 1
                        result = []
                    if cancel and cancel.is_set():
                        break
                    ret_array.extend(result)
                    if not observer:
                        self.progress.update(ptype='search', value=round(100 * (len(all_task) - len(pending)) / len(all_task)))
                    if observer:
                        observer(result, report)
            for future in pending:
                task_sites[future]['state'] = 'canceled' if cancel and cancel.is_set() else 'timeout'
            unfinished += len(pending)
            if pending and not (cancel and cancel.is_set()):
                log.warn(f"【{self._client_type.value}】{len(pending)} 个站点超时，已停止等待")
            # Running workers may finish after the deadline. Freeze the report
            # so their late statistics cannot rewrite this search's outcome.
            report = {key: dict(value) for key, value in report.items()}
            search_context['report'] = report
            if observer:
                observer([], report)
        finally:
            # Cancel only this call's queued work, never shut down the shared
            # executor or cancel another user's search. Running requests keep
            # their slots until their real work and session cleanup finish.
            for task in all_task:
                if task.cancel():
                    # 尚未执行便取消的查询不应阻止第二轮重试。
                    submitted.discard(task_queries[task])
        # 计算耗时
        end_time = datetime.datetime.now()
        log.info(f"【{self._client_type.value}】检索结束，有效资源数：%s，未完成站点：%s，总耗时 %s 秒"
                 % (len(ret_array), unfinished, (end_time - start_time).seconds))
        if not observer:
            self.progress.update(ptype='search', text="检索完成，有效资源数：%s，未完成站点：%s，总耗时 %s 秒"
                                                      % (len(ret_array), unfinished, (end_time - start_time).seconds),
                                 value=100)
        return ret_array
