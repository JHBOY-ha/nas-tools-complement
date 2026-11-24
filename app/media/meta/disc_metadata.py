"""Validate directory movie metadata before acknowledging a disc transfer."""
import os
import re
from xml.etree import ElementTree


def validate_movie_nfo(path, tmdb_id):
    """Missing files may be generated; existing files must prove the movie identity."""
    if not os.path.lexists(path):
        return False
    with open(path, 'rb') as stream:
        data = stream.read()
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as error:
        # Jellyfin also accepts an NFO containing only a provider URL. A release
        # description with an incidental link is not an identity-only NFO.
        match = re.fullmatch(rb'https?://(?:www\.)?themoviedb\.org/movie/([0-9]+)(?:[-/?#]\S*)?',
                             data.strip().removeprefix(b'\xef\xbb\xbf'))
        if not match:
            raise ValueError('原盘 NFO 无法解析，请核对后重试：%s' % path) from error
        identifiers = [match[1].decode('ascii')]
    else:
        if root.tag.lower() != 'movie':
            raise ValueError('原盘 NFO 不是电影元数据，请核对：%s' % path)
        identifiers = [(node.text or '').strip() for node in root
                       if node.tag.lower() == 'tmdbid' or (node.tag.lower() == 'uniqueid'
                           and node.get('type', '').strip().lower() == 'tmdb')]
        identifiers = [value for value in identifiers if value]
    if not identifiers:
        raise ValueError('原盘 NFO 缺少 TMDB ID，请核对并补充后重试：%s' % path)
    if any(not value.isascii() or not value.isdigit() or int(value) != int(tmdb_id)
           for value in identifiers):
        raise ValueError('原盘 NFO 的 TMDB ID 与已识别电影 %s 冲突，保留原文件：%s' % (tmdb_id, path))
    return True
