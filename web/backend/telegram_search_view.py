"""Read-only Telegram message rendering and shared preference filtering.

These functions consume session snapshots, with no persistence, transport or
job scheduling. Automatic selection uses the same filtered results as the view.
"""
import math
import time
from collections import Counter

from app.helper.search_sessions import ACTIVE_SEARCH_STATES, unpack_media
from app.utils import StringUtils

PAGE_SIZE = 5


def visible_results(record: dict) -> list[tuple[str, dict]]:
    prefs = record['preferences']
    items = [(rid, data) for rid, data in record['results'].items()
             if (not prefs['quality'] or prefs['quality'].lower() in str(data.get('resource_pix', '')).lower())
             and (not prefs['max_gb'] or float(data.get('size') or 0) <= prefs['max_gb'] * 1024 ** 3)
             and (not prefs['sites'] or data.get('site') in prefs['sites']
                  or any(source['site'] in prefs['sites'] for source in data.get('sources', [])))]
    return sorted(items, key=lambda item: (int(item[1].get('res_order') or 0),
                  int(item[1].get('site_order') or 0), int(item[1].get('seeders') or 0)), reverse=True)


def render_search_message(record: dict) -> tuple[str, list[list[dict[str, str]]]]:
    def button(label, action, argument='0'):
        return {'text': label, 'callback_data': f"nts:{record['id']}:{record['epoch']}:{action}:{argument}"}
    media = unpack_media(record['selected']) if record['selected'] else None
    title = media.get_title_string() if media else record['query']
    lines, rows = [title[:180], record['message']], []
    if media:
        lines += [f'{media.original_title or media.title} · 评分 {media.vote_average or "未知"}',
                  str(media.overview or '')[:240]]
    for rid, operation in list(record['downloads'].items())[-5:]:
        name = record['results'].get(rid, {}).get('org_string') or rid
        lines.append(f"下载任务 {str(name)[:70]}：{operation['message']}")
    if time.time() > record['expires_at']:
        rows = [[button('重新搜索', 'restart'), button('下载状态', 'status')]]
        if record['state'] in ACTIVE_SEARCH_STATES | {'failed', 'interrupted'}:
            rows.insert(0, [button('停止搜索', 'cancel')])
        return '\n'.join(lines + ['搜索已过期，请重新搜索。已提交的下载仍保留。']), rows
    if record['state'] in ('canceled', 'interrupted', 'failed'):
        rows = [[button('重新搜索', 'restart'), button('换一部', 'change')], [button('下载状态', 'status')]]
        if record['state'] != 'canceled':
            rows.insert(0, [button('停止搜索', 'cancel')])
        return '\n'.join(lines), rows
    view = record['view']
    if view == 'preferences':
        prefs = record['preferences']
        lines += ['筛选仅作用于当前结果；扩大站点范围后请重新搜索。',
                  '画质：' + (prefs['quality'] or '不限'),
                  '大小上限：' + (str(prefs['max_gb']) + ' GB' if prefs['max_gb'] else '不限'),
                  '站点：' + ('、'.join(prefs['sites']) or '全部')]
        rows = [[button('切换画质', 'quality'), button('切换大小', 'size')],
                [button('选择站点', 'sites'), button('下载设置', 'settings')],
                [button('保存为我的偏好', 'save')],
                [button('返回资源' if record['selected'] else '返回电影选择',
                        'results' if record['selected'] else 'candidates')]]
    elif view in ('sites', 'settings'):
        options = record.get('site_options' if view == 'sites' else 'setting_options', [])
        page = min(record['page'], max(0, math.ceil(len(options) / PAGE_SIZE) - 1))
        for index in range(page * PAGE_SIZE, min(len(options), (page + 1) * PAGE_SIZE)):
            label = options[index] if view == 'sites' else options[index]['name']
            rows.append([button(label[:50], 'site' if view == 'sites' else 'setting', str(index))])
        rows.append([button('上一页', 'page', str(max(0, page - 1))), button('下一页', 'page', str(page + 1))])
        rows.append([button('返回筛选', 'preferences')])
    elif view == 'candidates':
        count = len(record['candidates'])
        page = min(record['page'], max(0, math.ceil(count / PAGE_SIZE) - 1))
        for index in range(page * PAGE_SIZE, min(count, (page + 1) * PAGE_SIZE)):
            media = unpack_media(record['candidates'][index])
            lines.append(f'{index + 1}. {media.get_title_string()} / {media.original_title or ""} · 评分 {media.vote_average or "未知"}')
            rows.append([button(media.get_title_string()[:60], 'pick', str(index))])
        if count:
            rows.append([button('上一页', 'page', str(max(0, page - 1))), button('下一页', 'page', str(page + 1))])
        if record['more_candidates']:
            rows.append([button('查询更多候选', 'more')])
        if record['state'] == 'candidates':
            rows.append([button('搜索偏好', 'preferences')])
    elif view == 'detail':
        rid = record['detail']
        item = record['results'][rid]
        lines += [str(item.get('org_string', ''))[:700], str(item.get('description') or '')[:1000],
                  '音轨/字幕：请以发布说明为准，未确认信息不作推断']
        rows = [[button('下载这个资源', 'download', rid), button('返回资源', 'results')]]
        if len(item.get('sources', [])) > 1:
            for index, source in enumerate(item['sources']):
                rows.append([button(f"从 {source['site']} 下载", 'source', f'{rid}_{index}')])
        url = item.get('page_url') or ''
        if url.startswith(('https://', 'http://')):
            rows.append([{'text': '站点详情', 'url': url}])
    else:
        report = Counter(value['state'] for value in record['report'].values())
        completed = sum(report[value] for value in ('done', 'empty', 'filtered', 'failed', 'unsupported'))
        if report:
            lines.append(f"站点：{completed}/{len(record['report'])} 已返回；超时 {report['timeout']}；失败 {report['failed']}；繁忙 {report['busy']}")
            lines.append(f"无结果 {report['empty']}；过滤后为空 {report['filtered']}；片名不支持 {report['unsupported']}")
        items = visible_results(record)
        page = min(record['page'], max(0, math.ceil(len(items) / PAGE_SIZE) - 1))
        lines.append(f'有效资源 {len(items)} / 已收集 {len(record["results"])}，第 {page + 1} 页')
        if len(record['results']) >= 500:
            lines.append('已保留前 500 条资源，可缩小站点或关键词范围重新搜索。')
        if record['state'] in ACTIVE_SEARCH_STATES:
            lines.append('搜索尚未完成；开启自动下载时，会等待全部搜索结束。')
        for index, (rid, item) in enumerate(items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], start=page * PAGE_SIZE + 1):
            size = StringUtils.str_filesize(int(item.get('size') or 0))
            lines += [f"{index}. {str(item.get('org_string') or item.get('title'))[:150]}",
                      f"{size} · 做种 {item.get('seeders') or 0} · {item.get('site') or ''}"]
            operation = record['downloads'].get(rid)
            if operation:
                lines.append(operation['message'])
            retryable = not operation or operation['state'] == 'not_submitted'
            rows.append([button(f'{index} 下载' if retryable else f'{index} 查看状态',
                                'download' if retryable else 'status', rid), button('详情', 'detail', rid)])
        if items:
            rows.append([button('上一页', 'page', str(max(0, page - 1))), button('下一页', 'page', str(page + 1))])
        rows.append([button('筛选与偏好', 'preferences'), button('刷新结果', 'results')])
        if not items and record['selected'] and record['state'] not in ACTIVE_SEARCH_STATES | {'exists'}:
            rows.append([button('订阅这部作品', 'subscribe')])
    if record['state'] in ACTIVE_SEARCH_STATES:
        rows.append([button('停止搜索', 'cancel')])
    else:
        rows.append([button('重新搜索', 'restart'), button('换一部', 'change')])
    if record['selected'] and (record['selected'].get('poster_path') or '').startswith('https://'):
        rows.append([{'text': '查看海报', 'url': record['selected']['poster_path']}])
    if record['downloads']:
        rows.append([button('查询下载状态', 'status')])
    return '\n'.join(lines)[:3900], rows
