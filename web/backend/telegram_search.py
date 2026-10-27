"""Telegram search sessions: stable buttons, bounded jobs and durable submissions."""
import copy
import hashlib
import re
import threading
import time
import traceback
from typing import Union
from urllib.parse import parse_qs, urlparse

import log
from app.downloader import Downloader
from app.helper import ThreadHelper
from app.helper.search_sessions import ACTIVE_SEARCH_STATES, SearchSessions, pack_media, unpack_media
from app.indexer import Indexer
from app.media import Media
from app.message import Message
from app.searcher import Searcher
from app.subscribe import Subscribe
from app.utils.commons import singleton
from app.utils.types import MediaType, SearchType
from app.utils.workload import TaskQueueFull
from config import Config
from web.backend.web_utils import WebUtils
from web.backend.telegram_search_view import PAGE_SIZE, render_search_message, visible_results


def resource_key(media):
    # Only equal torrent identities are coalesced. A matching release title is
    # insufficient evidence that two downloads contain the same files.
    url = media.enclosure or ''
    hashes = parse_qs(urlparse(url).query).get('xt', []) if url.startswith('magnet:') else []
    identity = next((value.lower() for value in hashes if value.lower().startswith('urn:btih:')), url)
    return hashlib.sha256(identity.encode()).hexdigest()[:16]


SearchArgument = Union[int, str, tuple[str, int], None]


def _parse_callback(data: object) -> tuple[str, int, str, SearchArgument]:
    """Decode the wire protocol once; workers receive typed action arguments."""
    if not isinstance(data, str):
        raise ValueError('无效的搜索按钮，请重新搜索')
    try:
        prefix, sid, version, action, raw = data.split(':', 4)
        epoch = int(version)
        if prefix != 'nts' or not re.fullmatch(r'[0-9a-f]{16}', sid) or epoch < 0:
            raise ValueError
        if action in {'pick', 'page', 'site', 'setting'}:
            argument = int(raw)
            if argument < 0:
                raise ValueError
        elif action in {'detail', 'download', 'source'}:
            if action == 'source':
                rid, index = raw.rsplit('_', 1)
                argument = (rid, int(index))
                if argument[1] < 0:
                    raise ValueError
            else:
                rid, argument = raw, raw
            if not re.fullmatch(r'[0-9a-f]{16}', rid):
                raise ValueError
        elif action in {'cancel', 'restart', 'change', 'more', 'results', 'candidates',
                        'preferences', 'quality', 'size', 'save', 'sites', 'settings', 'status', 'subscribe'}:
            if raw != '0':
                raise ValueError
            argument = None
        else:
            raise ValueError
    except ValueError:
        raise ValueError('无效的搜索按钮，请重新搜索') from None
    return sid, epoch, action, argument


@singleton
class TelegramSearch:
    def __init__(self, store=None, helper=None):
        self.store = store or SearchSessions()
        self.helper = helper or ThreadHelper()
        self.cancel_events = {}
        self.publish_locks = {}
        self.lock = threading.RLock()

    def client(self):
        active = Message().get_interactive_client(SearchType.TG)
        if not active:
            raise ValueError('Telegram 交互已关闭')
        return active['client']

    def receive(self, text, owner, chat, user_name=None, request_id=None, reply_to=None):
        # Numeric film titles such as 1917 remain searchable. Longer numeric
        # choices belong to a candidate message only when explicitly replying.
        if text.isdigit() and (reply_to is not None or int(text) < 10):
            record = self.store.find_message(owner, chat, reply_to) if reply_to is not None else None
            if not record or record['state'] != 'candidates':
                self.client().search_message(chat, '请点击候选按钮，或回复对应候选消息的序号。', [])
                return
            self.action(record['id'], record['epoch'], 'pick', int(text) - 1, owner, chat)
            return
        query = re.sub(r'^(搜索|下载)[:：\s]*', '', text).strip()
        if not query or len(query) > 256:
            self.client().search_message(chat, '请输入片名和可选年份，例如：沙丘 2021', [])
            return
        record, created = self.store.create(owner, chat, query, request_id=request_id)
        if not created:
            self.publish(record['id'])
            return
        with self.store.edit(record['id']) as current:
            current['user_name'] = user_name
        self.enqueue(record['id'], self.identify, record['id'], 0)
        self.publish(record['id'])

    def enqueue(self, sid, function, *args, **kwargs):
        try:
            self.helper.start_thread(self.run_job, (sid, function, args, kwargs), pool='background')
        except TaskQueueFull:
            with self.store.edit(sid) as record:
                record['message'] = '任务队列繁忙，请稍后重试'
                if record['state'] in ACTIVE_SEARCH_STATES:
                    record['state'] = 'interrupted'
            self.publish(sid)

    def run_job(self, sid, function, args, kwargs):
        try:
            function(*args, **kwargs)
        except Exception as error:
            # Do not reflect token-bearing provider URLs or torrent credentials.
            frames = traceback.extract_tb(error.__traceback__)
            location = ' → '.join(f'{frame.name}:{frame.lineno}' for frame in frames)
            log.error(f'【Telegram Search】{function.__name__}: {type(error).__name__} ({location})')
            with self.store.edit(sid) as record:
                record['message'] = '操作失败，请查看日志或稍后重试'
                if record['state'] in ACTIVE_SEARCH_STATES:
                    record['state'] = 'failed'
            self.publish(sid)

    def identify(self, sid, epoch, page=1):
        record = self.store.get(sid)
        self.publish(sid)
        candidates = WebUtils.search_media_infos(record['query'], page=page)
        with self.store.edit(sid) as current:
            if current['epoch'] != epoch or current['state'] == 'canceled':
                return
            known = {(value.get('type'), value.get('tmdb_id'), value.get('douban_id'), value.get('title'), value.get('year'))
                     for value in current['candidates']}
            incoming = []
            for media in candidates:
                key = (media.type, media.tmdb_id, media.douban_id, media.title, media.year)
                if key not in known:
                    known.add(key)
                    incoming.append(pack_media(media))
            incoming = incoming[:max(0, 100 - len(current['candidates']))]
            current['candidates'].extend(incoming)
            current.update(state='candidates', candidate_page=page,
                           more_candidates=bool(incoming) and len(current['candidates']) < 100, view='candidates',
                           page=(len(current['candidates']) - len(incoming)) // PAGE_SIZE,
                           message='请选择电影' if current['candidates'] else '没有找到电影，请修改片名或年份')
        if page == 1 and len(candidates) == 1:
            self.action(sid, epoch, 'pick', 0, record['owner'], record['chat'])
        else:
            self.publish(sid)

    def callback(self, query):
        owner = query['from']['id']
        chat = query.get('message', {}).get('chat', {}).get('id')
        try:
            sid, epoch, action, argument = _parse_callback(query.get('data'))
            self.store.owned(sid, owner, chat, epoch, channel='telegram')
            with self.store.edit(sid) as record:
                seen = record.setdefault('callbacks', [])
                duplicate = query['id'] in seen
                if not duplicate:
                    record['callbacks'] = (seen + [query['id']])[-64:]
            if duplicate:
                self.client().answer_search_callback(query['id'], '此操作已接收')
                return
            # Cancellation must remain available even when background searches
            # occupy every worker. Other callbacks use short interactive jobs.
            if action == 'cancel':
                self.action(sid, epoch, action, argument, owner, chat)
            else:
                try:
                    self.helper.start_thread(self.action, (sid, epoch, action, argument, owner, chat),
                                             pool='interactive')
                except TaskQueueFull:
                    with self.store.edit(sid) as record:
                        record['callbacks'].remove(query['id'])
                    raise
            self.client().answer_search_callback(query['id'])
        except (ValueError, TaskQueueFull) as error:
            self.client().answer_search_callback(query['id'], str(error)[:180])

    def action(self, sid: str, epoch: int, action: str, argument: SearchArgument, owner, chat):
        try:
            # Recheck ownership/version after queueing: the session can advance
            # while a callback waits, even though its wire data is already valid.
            record = self.store.owned(sid, owner, chat, epoch, channel='telegram')
            if time.time() > record['expires_at'] and action not in ('restart', 'status'):
                self.publish(sid)
                return
            if action == 'cancel':
                with self.store.edit(sid) as current:
                    current.update(state='canceled', message='搜索已取消；已提交的下载不受影响')
                with self.lock:
                    event = self.cancel_events.get((sid, epoch))
                    if event:
                        event.set()
            elif action == 'pick':
                index = argument
                if record['state'] != 'candidates' or not 0 <= index < len(record['candidates']):
                    raise ValueError('请使用当前候选按钮')
                with self.store.edit(sid) as current:
                    if current['epoch'] != epoch or current['state'] != 'candidates':
                        return
                    current.update(selected=current['candidates'][index], state='queued',
                                   epoch=epoch + 1, view='results', page=0, message='已选定电影，正在准备搜索')
                self.enqueue(sid, self.search, sid, epoch + 1)
            elif action in ('restart', 'change'):
                if record['state'] in ACTIVE_SEARCH_STATES:
                    raise ValueError('请先取消当前搜索')
                # A new session keeps old result buttons and download receipts
                # bound to the old resource instead of recycling its IDs.
                new, _ = self.store.create(owner, chat, record['query'])
                with self.store.edit(new['id']) as current:
                    current['user_name'] = record['user_name']
                    current['preferences'] = record['preferences']
                    if action == 'restart' and record['selected']:
                        current.update(selected=record['selected'], state='queued', view='results')
                self.enqueue(new['id'], self.search if action == 'restart' and record['selected'] else self.identify,
                             new['id'], 0)
                return
            elif action == 'more':
                if record['state'] != 'candidates' or not record['more_candidates']:
                    return
                with self.store.edit(sid) as current:
                    if current['state'] != 'candidates':
                        return
                    current['state'] = 'identifying'
                self.enqueue(sid, self.identify, sid, epoch, record['candidate_page'] + 1)
            elif action in ('page', 'results', 'candidates', 'preferences'):
                with self.store.edit(sid) as current:
                    if action == 'page':
                        current['page'] = argument
                    else:
                        current.update(view=action, page=0)
            elif action in ('quality', 'size', 'save', 'site', 'setting'):
                with self.store.edit(sid) as current:
                    prefs = current['preferences']
                    if action == 'quality':
                        choices = ['', '1080p', '2160p']
                        prefs['quality'] = choices[(choices.index(prefs['quality']) + 1) % len(choices)]
                    elif action == 'size':
                        choices = [0, 10, 30, 60]
                        prefs['max_gb'] = choices[(choices.index(prefs['max_gb']) + 1) % len(choices)]
                    elif action == 'site':
                        names = current.get('site_options', [])
                        name = names[argument]
                        prefs['sites'] = [] if name == '全部站点' else [name]
                    elif action == 'setting':
                        options = current.get('setting_options', [])
                        prefs['setting'] = options[argument]['id']
                    current.update(page=0, view='preferences')
                    if action == 'save':
                        current['message'] = '偏好已保存，下次搜索自动使用'
                if action == 'save':
                    self.store.preferences(owner, value=self.store.get(sid)['preferences'])
            elif action in ('sites', 'settings'):
                if action == 'sites':
                    names = ['全部站点'] + Indexer().get_indexer_names()
                    with self.store.edit(sid) as current:
                        current.update(site_options=names, view='sites', page=0)
                else:
                    options = [{'id': None, 'name': '站点/系统默认'}] + [
                        {'id': key, 'name': value.get('name') or str(key)}
                        for key, value in Downloader().get_download_setting().items()]
                    with self.store.edit(sid) as current:
                        current.update(setting_options=options, view='settings', page=0)
            elif action == 'detail':
                if argument not in record['results']:
                    raise ValueError('资源已失效')
                with self.store.edit(sid) as current:
                    current.update(view='detail', detail=argument)
            elif action in ('download', 'source'):
                source_index = None
                if action == 'source':
                    argument, source_index = argument
                    if not 0 <= source_index < len(record['results'].get(argument, {}).get('sources', [])):
                        raise ValueError('站点来源已失效')
                if argument not in record['results'] or record['state'] in ('canceled', 'failed', 'interrupted'):
                    raise ValueError('请重新搜索后下载')
                self.enqueue(sid, self.download, sid, epoch, argument, source_index=source_index)
            elif action == 'status':
                self.enqueue(sid, self.refresh_downloads, sid)
            elif action == 'subscribe':
                if not record['selected'] or record['state'] in ACTIVE_SEARCH_STATES:
                    raise ValueError('请先完成电影搜索')
                self.enqueue(sid, self.subscribe, sid)
            else:
                raise ValueError('不支持的操作')
            self.publish(sid)
        except ValueError as error:
            self.client().search_message(chat, str(error), [])
        except IndexError:
            self.client().search_message(chat, '所选项目已变化，请刷新后重试', [])

    def search(self, sid, epoch):
        record = self.store.get(sid)
        media = unpack_media(record['selected'])
        if not media.tmdb_id:
            # Douban candidates must resolve to a concrete TMDB work before any
            # resource request; preserve the user-selected title/year constraint.
            media = Media().get_media_info(title=media.get_title_string(), mtype=media.type, strict=True)
            if not media or not media.tmdb_id:
                raise ValueError('无法匹配 TMDB 电影')
        downloader = Downloader()
        exists, no_exists, messages = downloader.check_exists_medias(meta_info=media)
        with self.store.edit(sid) as current:
            if current['epoch'] != epoch or current['state'] == 'canceled':
                return
            current['selected'] = pack_media(media)
            current.update(state='exists' if exists else 'searching',
                           message='媒体库中已有这部作品' if exists else '正在搜索资源…')
        self.publish(sid)
        if exists:
            return
        cancel = threading.Event()
        with self.lock:
            self.cancel_events[(sid, epoch)] = cancel
        last_update = 0.0
        collected = {}
        def progress(results, report, flush=False):
            nonlocal last_update
            current = self.store.get(sid)
            if current['epoch'] != epoch or current['state'] != 'searching':
                cancel.set()
                return
            for item in results:
                rid = resource_key(item)
                if rid not in collected and len(collected) < 500:
                    collected[rid] = pack_media(item)
                    collected[rid]['sources'] = []
                if rid in collected:
                    source = {key: getattr(item, key) for key in ('site', 'enclosure', 'page_url',
                              'site_order', 'seeders', 'upload_volume_factor', 'download_volume_factor')}
                    sources = collected[rid]['sources']
                    if not any((value['site'], value['enclosure']) == (source['site'], source['enclosure'])
                               for value in sources) and len(sources) < 10:
                        sources.append(source)
            # Persist and edit in batches, not once for every site completion.
            if not flush and time.monotonic() - last_update < 2:
                return
            with self.store.edit(sid) as current:
                if current['state'] != 'searching':
                    cancel.set()
                    return
                current.update(report={key: dict(value) for key, value in report.items()}, results=copy.deepcopy(collected),
                               message='正在搜索资源…')
            last_update = time.monotonic()
            self.publish(sid)
        context = {'cancel_event': cancel, 'on_progress': progress}
        searcher = Searcher()
        try:
            searcher.search_one_media(media, SearchType.TG, no_exists,
                                       sites=record['preferences']['sites'] or media.search_sites,
                                       search_context=context, search_only=True)
            # Use only provider-backed alternative names, only after no valid
            # result, and retain the same dispatch context to avoid repeat calls.
            if not context.get('results') and not cancel.is_set():
                aliases = (media.tmdb_info or {}).get('alternative_titles') or {}
                provider = searcher.media.movie if media.type == MediaType.MOVIE else searcher.media.tv
                if not aliases and provider:
                    try:
                        details = provider.details(media.tmdb_id, append_to_response='alternative_titles', language='en-US')
                        aliases = details.get('alternative_titles') or {}
                    except Exception as error:
                        log.warn(f'【Telegram Search】别名查询失败：{type(error).__name__}')
                titles = aliases.get('titles') or aliases.get('results') or []
                known = {name.casefold() for name in (media.title, media.cn_name, media.original_title,
                                                     context.get('english_word')) if name}
                names = []
                for value in titles:
                    alias = value.get('title')
                    if alias and value.get('iso_3166_1') in ('US', 'GB', 'CN') and alias.casefold() not in known:
                        known.add(alias.casefold())
                        names.append(alias)
                for alias in names[:2]:
                    found = searcher.search_medias(alias, {'year': media.year, 'type': media.type,
                        'site': record['preferences']['sites'], 'seeders': True}, media, SearchType.TG,
                        search_context=context)
                    if found or cancel.is_set():
                        break
            progress([], context.get('report', {}), flush=True)
            with self.store.edit(sid) as current:
                if cancel.is_set() or current['state'] != 'searching':
                    return
                current.update(state='results', message='搜索完成' if current['results'] else '没有找到有效资源')
            self.publish(sid)
            settings = Config().get_config('pt')
            if settings.get('search_no_result_rss') and not self.store.get(sid)['results']:
                self.subscribe(sid)
            if settings.get('search_auto', True):
                items = [unpack_media(value) for _, value in visible_results(self.store.get(sid))]
                # Reuse the existing ranking and select only after all rounds.
                for item in downloader.get_download_list(items):
                    self.download(sid, epoch, resource_key(item), automatic=True, no_exists=no_exists)
        finally:
            with self.lock:
                self.cancel_events.pop((sid, epoch), None)


    def download(self, sid, epoch, rid, automatic=False, no_exists=None, source_index=None):
        # The durable claim precedes downloader I/O. A timeout stays unknown;
        # neither a second click nor a restart is allowed to resubmit it.
        with self.store.edit(sid) as record:
            if record['epoch'] != epoch or record['state'] in ('canceled', 'failed', 'interrupted'):
                return
            if rid in record['downloads'] and record['downloads'][rid]['state'] != 'not_submitted':
                return
            media = unpack_media(record['results'][rid])
            sources = record['results'][rid].get('sources', [])
            if source_index is not None:
                media.__dict__.update(sources[source_index])
            elif record['preferences']['sites'] and media.site not in record['preferences']['sites']:
                source = next((value for value in sources if value['site'] in record['preferences']['sites']), None)
                if source:
                    media.__dict__.update(source)
            setting = record['preferences']['setting'] or media.download_setting
            tag = 'nts-' + hashlib.sha256((record['owner'] + rid + str(setting)).encode()).hexdigest()[:24]
            prior = self.store.find_download(tag)
            record['downloads'][rid] = prior if prior else {
                'tag': tag, 'state': 'submitting', 'message': '正在提交下载', 'setting': setting,
                'site': media.site, 'task_id': None}
            if prior:
                return
            media.user_name = record['user_name']
        self.publish(sid)
        downloader = Downloader()
        operation = self.store.get(sid)['downloads'][rid]
        attempted = False
        try:
            status = downloader.get_search_task_status(operation['tag'], setting, media.site)
            if status.get('lookup_failed'):
                operation.update(state='not_submitted', message='下载器查询失败，尚未提交；可稍后重试下载')
            elif status.get('task_id'):
                operation.update(status)
            else:
                exists, _, _ = downloader.check_exists_medias(meta_info=media)
                if exists:
                    operation.update(state='exists', message='媒体库中已存在，未重复下载')
                else:
                    attempted = True
                    if automatic:
                        media.download_setting = setting
                        downloaded, _ = downloader.batch_download(SearchType.TG, [media],
                            need_tvs=no_exists, user_name=record['user_name'], task_tag=tag)
                        result = bool(downloaded)
                    else:
                        result, _ = downloader.download(media, download_setting=setting, task_tag=tag)
                    operation.update(state='accepted' if result else 'unknown',
                        message='已提交到下载器' if result else '提交状态待确认，请查询下载器后处理')
                    # Confirmation is durable independently of later status
                    # lookups. The existing poller (or status button) resolves
                    # the task ID; a query failure must not undo acceptance.
        except Exception as error:
            log.error(f'【Telegram Search】下载{"提交" if attempted else "检查"}失败：{type(error).__name__}')
            operation.update(state='unknown' if attempted else 'not_submitted',
                             message='提交状态待确认，请查询下载器；不会自动重复提交' if attempted
                             else '下载前检查失败，尚未提交；可稍后重试下载')
        with self.store.edit(sid) as record:
            record['downloads'][rid] = operation
        self.publish(sid)

    def refresh_downloads(self, sid, background=False):
        record = self.store.get(sid)
        changed = False
        operations = sorted(((rid, op) for rid, op in record['downloads'].items()
                             if op['state'] not in ('organized', 'exists', 'not_submitted')),
                            key=lambda item: item[1].get('checked_at', 0))
        for rid, operation in operations[:4 if background else 20]:
            status = Downloader().get_search_task_status(operation['tag'], operation['setting'], operation['site'])
            with self.store.edit(sid) as current:
                current['downloads'][rid]['checked_at'] = time.time()
                if status.get('task_id'):
                    current['downloads'][rid].update(status)
                    changed = changed or operation['state'] != status['state']
        if changed or not background:
            self.publish(sid)

    def refresh_pending(self):
        """Use the existing scheduler budget; notify only task state changes."""
        if not Message().get_interactive_client(SearchType.TG):
            return
        for sid in self.store.pending_download_sessions('telegram', limit=5):
            self.refresh_downloads(sid, background=True)

    def subscribe(self, sid):
        with self.store.edit(sid) as record:
            if record.get('subscription'):
                return
            record['subscription'] = 'submitting'
        media = unpack_media(record['selected'])
        code, message, _ = Subscribe().add_rss_subscribe(
            mtype=media.type, name=media.title, year=media.year, season=media.begin_season,
            mediaid=media.tmdb_id, state='R', rss_sites=media.rss_sites,
            search_sites=record['preferences']['sites'] or media.search_sites)
        with self.store.edit(sid) as current:
            current['subscription'] = 'accepted' if code == 0 else None
            current['message'] = '已添加订阅' if code == 0 else '订阅失败：' + str(message)
        self.publish(sid)

    def publish(self, sid):
        with self.lock:
            if len(self.publish_locks) > 100:
                self.publish_locks = {key: gate for key, gate in self.publish_locks.items()
                                      if key in self.store or gate.locked()}
            gate = self.publish_locks.setdefault(sid, threading.Lock())
        with gate:
            record = self.store.get(sid)
            text, keyboard = render_search_message(record)
            try:
                message_id = self.client().search_message(record['chat'], text, keyboard, record['message_id'])
            except RuntimeError as error:
                log.warn(f'【Telegram Search】状态消息未送达：{error}')
                return
            if message_id != record['message_id']:
                with self.store.edit(sid) as current:
                    current['message_id'] = message_id
