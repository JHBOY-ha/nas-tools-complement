"""Bounded season archive inspection; members are streamed, never extracted by name."""
import hashlib
import json
import os
import re
import stat
import unicodedata
import zipfile
from contextlib import contextmanager

TEXT_EXTENSIONS = {'.srt', '.ass', '.ssa', '.vtt', '.smi'}


def _number(text):
    if text.isdigit():
        return int(text)
    digits = dict(zip('零一二三四五六七八九', range(10)))
    if '十' in text:
        left, right = text.split('十', 1)
        return digits.get(left, 1) * 10 + digits.get(right, 0)
    return digits.get(text, -1)


def episode_key(name, selected_season):
    """Require one episode, honour explicit season and reject ranges/ambiguity."""
    name = unicodedata.normalize('NFKC', name.replace('\\', '/'))
    seasons = {int(n) for n in re.findall(r'(?i)(?<![a-z0-9])s(?:eason[ ._-]*)?(\d{1,3})(?!\d)', name)}
    seasons.update(_number(n) for n in re.findall(r'第([零一二三四五六七八九十\d]+)季', name))
    pairs = re.findall(r'(?i)(?<!\d)(\d{1,3})x(\d{1,4})(?!\d)', name)
    seasons.update(int(s) for s, _ in pairs)
    episodes = {int(e) for _, e in pairs}
    episodes.update(int(n) for n in re.findall(r'(?i)(?<![a-z])e(?:p)?[ ._-]?(\d{1,4})(?!\d)', name))
    episodes.update(_number(n) for n in re.findall(r'第([零一二三四五六七八九十\d]+)[集话話]', name))
    # Multi-episode subtitles cannot be safely split into separate videos.
    if re.search(r'(?i)(?:e(?:p)?[ ._-]?\d+|\d+x\d+|第[零一二三四五六七八九十\d]+)[ .]*[-~至到][ .]*(?:e(?:p)?|第)?\d+', name):
        return None, '多集或集数范围，无法自动拆分'
    if len(seasons) > 1 or (seasons and seasons != {selected_season}):
        return None, '季编号与所选季不一致'
    if not episodes:
        stem = os.path.splitext(name.rsplit('/', 1)[-1])[0]
        # Common anime [01], "Show - 01" and bare "01.zh" names; never guess
        # from arbitrary numbers such as release years, 1080p or checksums.
        bracketed = re.findall(r'(?<!\d)\[(\d{1,3})\](?!\d)', stem)
        suffix = re.search(r'(?:^|\s-\s)(\d{1,3})(?=$|[ ._](?:chs|cht|zh|en|简|繁)|\s*\[)', stem, re.I)
        episodes.update(int(n) for n in bracketed)
        if suffix:
            episodes.add(int(suffix[1]))
    if len(episodes) != 1:
        return None, '未识别到唯一集数'
    return (selected_season, next(iter(episodes))), ''


@contextmanager
def open_pack(stream, policy):
    stream.seek(0)
    if zipfile.is_zipfile(stream):
        archive = zipfile.ZipFile(stream)
        is_zip = True
    else:
        stream.seek(0)
        if not stream.read(8).startswith(b'Rar!\x1a\x07'):
            raise ValueError('请选择 ZIP 或 RAR 字幕包')
        import rarfile
        stream.seek(0)
        archive = rarfile.RarFile(stream)
        is_zip = False
    with archive:
        entries = archive.infolist()
        if len(entries) > 1000:
            raise ValueError('字幕包最多包含 1000 个文件和目录')
        total = 0
        seen = set()
        members = []
        for entry in entries:
            name = entry.filename
            if is_zip and not entry.flag_bits & 0x800:
                try:
                    name = name.encode('cp437').decode('gb18030')
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            name = name.replace('\\', '/')
            if (entry.is_dir() if is_zip else entry.isdir()):
                continue
            if name.startswith('/') or re.match(r'^[a-zA-Z]:', name) or '..' in name.split('/') or '\x00' in name:
                raise ValueError('字幕包含有不安全的文件路径')
            mode = (entry.external_attr >> 16) if is_zip else 0
            if (is_zip and stat.S_ISLNK(mode)) or (not is_zip and (entry.is_symlink() or entry.needs_password())):
                raise ValueError('不支持链接文件或加密字幕包')
            if is_zip and entry.flag_bits & 1:
                raise ValueError('不支持加密字幕包')
            key = unicodedata.normalize('NFC', name).casefold()
            if key in seen:
                raise ValueError('字幕包包含重复文件名')
            seen.add(key)
            total += entry.file_size
            if total > int(policy['batch_limit_mb']) * 1024 * 1024:
                raise ValueError('字幕包解压后超过单批总量限制')
            if os.path.splitext(name)[1].lower() not in TEXT_EXTENSIONS or name.startswith('__MACOSX/'):
                continue
            if entry.file_size > int(policy['text_file_limit_mb']) * 1024 * 1024:
                raise ValueError('字幕包中有文件超过文本字幕大小限制')
            if len(members) >= 200:
                raise ValueError('字幕包最多包含 200 个文本字幕')
            members.append((name, entry))
        if not members:
            raise ValueError('字幕包中没有 SRT/ASS/SSA/VTT/SMI 文本字幕')
        yield archive, members


def build_plan(members, episodes, season, archive_hash):
    targets = {}
    for episode in episodes:
        try:
            key = (int(episode['season']), int(episode['episode']))
        except (KeyError, TypeError, ValueError):
            continue
        if key[0] == season and episode.get('path'):
            targets.setdefault(key, {})[episode['path']] = {field: episode.get(field) for field in ('path', 'season', 'episode', 'server_item_id', 'parent_server_item_id', 'library_id')}
    rows = []
    for index, (name, entry) in enumerate(members):
        key, reason = episode_key(name, season)
        candidates = list(targets.get(key, {}).values()) if key else []
        if key and len(candidates) != 1:
            reason = '该集存在多个视频版本' if candidates else '媒体库中未找到该集'
        target = candidates[0] if len(candidates) == 1 and not reason else None
        rows.append(dict(id=str(index), name=name, size=entry.file_size,
                         target=target, reason=reason,
                         season_episode=f'S{key[0]:02d}E{key[1]:02d}' if key else ''))
    digest = hashlib.sha256(json.dumps(
        [archive_hash, season, rows], sort_keys=True, ensure_ascii=False
    ).encode()).hexdigest()
    return dict(plan_id=digest, rows=rows)


class MemberStream:
    """Open only while consumed so RAR helpers do not run once per queued member."""
    def __init__(self, archive, entry):
        self.archive = archive
        self.entry = entry
        self.reader = None
        self.finished = False

    def read(self, size=-1):
        if self.finished:
            return b''
        if self.reader is None:
            self.reader = self.archive.open(self.entry)
        data = self.reader.read(size)
        if not data:
            self.close()
        return data

    def close(self):
        if self.reader is not None:
            self.reader.close()
            self.reader = None
        self.finished = True
