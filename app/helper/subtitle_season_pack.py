"""Bounded season pack handling for ZIP archives and multi-file subtitle uploads."""
import hashlib
import json
import os
import re
import stat
import unicodedata
import zipfile
from contextlib import contextmanager

TEXT_EXTENSIONS = {'.srt', '.ass', '.ssa', '.vtt', '.smi'}
RAR_SIGNATURE = b'Rar!\x1a\x07'
MAX_MEMBERS = 200
RAR_HINT = 'RAR 字幕包请先解压，再选择其中的字幕文件上传'
PACK_HINT = '请选择 ZIP 字幕包，或直接多选 SRT/ASS/SSA/VTT/SMI 字幕文件'


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
def open_season_source(files, policy):
    """Yield (source, members) for one ZIP pack or a multi-file subtitle selection."""
    if len(files) == 1:
        stream = _upload_stream(files[0])
        stream.seek(0)
        if zipfile.is_zipfile(stream):
            with open_pack(stream, policy) as result:
                yield result
            return
    source, members = file_source(files, policy)
    with source:
        yield source, members


@contextmanager
def open_pack(stream, policy):
    """Yield (archive, members) for one uploaded ZIP字幕包."""
    stream.seek(0)
    if not zipfile.is_zipfile(stream):
        stream.seek(0)
        if stream.read(len(RAR_SIGNATURE)) == RAR_SIGNATURE:
            raise ValueError(RAR_HINT)
        raise ValueError(PACK_HINT)
    with zipfile.ZipFile(stream) as archive:
        yield archive, _pack_members(archive, policy)


def file_source(files, policy):
    """Validate subtitles uploaded on their own and return (source, members)."""
    if not files:
        raise ValueError(PACK_HINT)
    if len(files) > MAX_MEMBERS:
        raise ValueError(f'一次最多上传 {MAX_MEMBERS} 个字幕文件')
    members = []
    seen = set()
    total = 0
    for upload in files:
        stream = _upload_stream(upload)
        name = os.path.basename(str(getattr(upload, 'filename', '') or '')).replace('\\', '/').strip()
        if not name:
            raise ValueError('字幕文件名无效')
        extension = os.path.splitext(name)[1].lower()
        stream.seek(0)
        if extension == '.rar' or stream.read(len(RAR_SIGNATURE)) == RAR_SIGNATURE:
            raise ValueError(RAR_HINT)
        if extension not in TEXT_EXTENSIONS:
            raise ValueError(PACK_HINT)
        size = stream.seek(0, os.SEEK_END)
        stream.seek(0)
        if size > int(policy['text_file_limit_mb']) * 1024 * 1024:
            raise ValueError(f'{name} 超过文本字幕大小限制')
        total += size
        if total > int(policy['batch_limit_mb']) * 1024 * 1024:
            raise ValueError('上传的字幕总量超过单批总量限制')
        key = unicodedata.normalize('NFC', name).casefold()
        if key in seen:
            raise ValueError('存在重名的字幕文件')
        seen.add(key)
        members.append((name, FileMember(name, stream, size)))
    return FileSource(members), members


def _upload_stream(upload):
    return getattr(upload, 'stream', upload)


def _pack_members(archive, policy):
    """List text subtitle members of a ZIP archive while enforcing the batch policy."""
    entries = archive.infolist()
    if len(entries) > 1000:
        raise ValueError('字幕包最多包含 1000 个文件和目录')
    total = 0
    seen = set()
    members = []
    for entry in entries:
        name = entry.filename
        if not entry.flag_bits & 0x800:
            try:
                name = name.encode('cp437').decode('gb18030')
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
        name = name.replace('\\', '/')
        if entry.is_dir():
            continue
        if name.startswith('/') or re.match(r'^[a-zA-Z]:', name) or '..' in name.split('/') or '\x00' in name:
            raise ValueError('字幕包含有不安全的文件路径')
        if stat.S_ISLNK(entry.external_attr >> 16):
            raise ValueError('不支持链接文件')
        if entry.flag_bits & 1:
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
        if len(members) >= MAX_MEMBERS:
            raise ValueError(f'字幕包最多包含 {MAX_MEMBERS} 个文本字幕')
        members.append((name, entry))
    if not members:
        raise ValueError('字幕包中没有 SRT/ASS/SSA/VTT/SMI 文本字幕')
    return members


class FileMember:
    """Entry-alike for a subtitle file that was uploaded on its own."""

    def __init__(self, name, stream, size):
        self.filename = name
        self.stream = stream
        self.file_size = size


class FileSource:
    """Members that arrived as individual subtitle files."""

    def __init__(self, members):
        self._members = members

    def infolist(self):
        return [entry for _, entry in self._members]

    def open(self, entry):
        entry.stream.seek(0)
        return UploadHandle(entry.stream)

    def read(self, entry):
        with self.open(entry) as stream:
            chunks = []
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    return b''.join(chunks)
                chunks.append(chunk)

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


class UploadHandle:
    """Read handle for a request-owned stream; closing it must not release the upload."""

    def __init__(self, stream):
        self._stream = stream

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def __getattr__(self, name):
        return getattr(self._stream, name)


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
    """Open archive members only while they are consumed, then release them."""

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
