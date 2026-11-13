import os
import re


class PathUtils:

    @staticmethod
    def get_dir_files(in_path, exts="", filesize=0, episode_format=None, raise_errors=False, diagnostics=None,
                      include_bluray=False):
        """
        获得目录下的媒体文件列表List ，按后缀、大小、格式过滤
        diagnostics 可选：记录扫描数量及每个文件首次被排除的原因，不额外扫描目录。
        include_bluray 为 True 时返回原盘根目录并停止扫描其内部文件，不对片段套用大小和集号过滤。
        """
        if diagnostics is not None:
            diagnostics.update(scanned=0, invalid_path=0, unsupported_extension=0,
                               episode_mismatch=0, below_min_size=0)
        if not in_path:
            return []
        # 整理入口需要区分真实空目录与 NAS/权限故障；其他调用保留历史行为。
        if raise_errors:
            os.stat(in_path)
        if not os.path.exists(in_path):
            return []
        ret_list = []
        def count(reason):
            if diagnostics is not None:
                diagnostics[reason] += 1

        def eligible(path):
            count("scanned")
            if PathUtils.is_invalid_path(path):
                count("invalid_path")
                return False
            # Sidecars are not episode candidates; classify their suffix before
            # applying a media-specific pattern or validating its parsed range.
            if exts and os.path.splitext(path)[-1].lower() not in exts:
                count("unsupported_extension")
                return False
            if episode_format and not episode_format.match(os.path.basename(path)):
                count("episode_mismatch")
                return False
            if filesize and os.path.getsize(path) < filesize:
                count("below_min_size")
                return False
            return True

        def scan_error(error):
            if raise_errors:
                raise error
        if os.path.isdir(in_path):
            for root, dirs, files in os.walk(in_path, onerror=scan_error):
                if include_bluray:
                    bluray = PathUtils.get_bluray_dir(root)
                    if bluray:
                        count("scanned")
                        if PathUtils.is_invalid_path(bluray):
                            count("invalid_path")
                        elif bluray not in ret_list:
                            ret_list.append(bluray)
                        dirs[:] = []
                        continue
                for file in files:
                    cur_path = os.path.join(root, file)
                    if not eligible(cur_path):
                        continue
                    # 命中
                    if cur_path not in ret_list:
                        ret_list.append(cur_path)
        else:
            if not eligible(in_path):
                return []
            ret_list.append(in_path)
        return ret_list

    @staticmethod
    def get_dir_level1_files(in_path, exts=""):
        """
        查询目录下的文件（只查询一级）
        """
        ret_list = []
        if not os.path.exists(in_path):
            return []
        for file in os.listdir(in_path):
            path = os.path.join(in_path, file)
            if os.path.isfile(path):
                file_ext = os.path.splitext(file)[-1].lower()
                # 跳过没有扩展名的文件（如 .DS_Store）
                if exts and not file_ext:
                    continue
                if not exts or file_ext in exts:
                    ret_list.append(path)
        return ret_list

    @staticmethod
    def get_dir_level1_medias(in_path, exts=""):
        """
        根据后缀，返回目录下所有的文件及文件夹列表（只查询一级）
        """
        ret_list = []
        if not os.path.exists(in_path):
            return []
        if os.path.isdir(in_path):
            for file in os.listdir(in_path):
                path = os.path.join(in_path, file)
                if os.path.isfile(path):
                    if not exts or os.path.splitext(file)[-1].lower() in exts:
                        ret_list.append(path)
                else:
                    ret_list.append(path)
        else:
            ret_list.append(in_path)
        return ret_list

    @staticmethod
    def is_invalid_path(path):
        """
        判断是否不能处理的路径
        """
        if not path:
            return True
        if path.find('/@Recycle/') != -1 or path.find('/#recycle/') != -1 or path.find('/.') != -1 or path.find(
                '/@eaDir') != -1:
            return True
        return False

    @staticmethod
    def is_path_in_path(path1, path2):
        """
        判断两个路径是否包含关系 path1 in path2
        """
        if not path1 or not path2:
            return False
        path1 = os.path.normpath(path1)
        path2 = os.path.normpath(path2)
        if path1 == path2:
            return True
        path = os.path.dirname(path2)
        while True:
            if path == path1:
                return True
            path = os.path.dirname(path)
            if path == os.path.dirname(path):
                break
        return False

    @staticmethod
    def get_bluray_dir(path):
        """
        将原盘目录或其内部任意条目归一化为根目录；电影合集不是一张原盘。
        """
        if not path or not os.path.exists(path):
            return None
        path = os.path.normpath(path)
        directory = path if os.path.isdir(path) else os.path.dirname(path)
        if PathUtils._is_bluray_disc_set(directory):
            return directory
        # Resolve the nearest real disc ancestor, including PLAYLIST/CERTIFICATE
        # and index.bdmv events. Never infer a disc from arbitrarily named children.
        while directory:
            if os.path.isfile(os.path.join(directory, "BDMV", "index.bdmv")):
                parent = os.path.dirname(directory)
                if (re.fullmatch(r"(?:disc|disk)[ ._-]*\d+", os.path.basename(directory), re.IGNORECASE)
                        and PathUtils._is_bluray_disc_set(parent)):
                    return parent
                return directory
            parent = os.path.dirname(directory)
            if parent == directory:
                break
            directory = parent
        return None

    @staticmethod
    def _is_bluray_disc_set(path):
        """Only explicit Disc1/Disc2-style siblings may share their parent's identity."""
        if not path or not os.path.isdir(path):
            return False
        from config import RMT_MEDIAEXT
        discs = []
        for name in os.listdir(path):
            child = os.path.join(path, name)
            if PathUtils.is_invalid_path(child):
                continue
            if os.path.isdir(child):
                if (not re.fullmatch(r"(?:disc|disk)[ ._-]*\d+", name, re.IGNORECASE)
                        or not os.path.isfile(os.path.join(child, "BDMV", "index.bdmv"))):
                    return False
                discs.append(child)
            elif os.path.splitext(name)[1].lower() in RMT_MEDIAEXT:
                # A separate movie file beside the discs makes this a mixed folder.
                return False
        return bool(discs)

    @staticmethod
    def get_parent_paths(path, level: int = 1):
        """
        获取父目录路径，level为向上查找的层数
        """
        for lv in range(0, level):
            path = os.path.dirname(path)
        return path
