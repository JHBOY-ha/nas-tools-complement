import datetime
import os
import platform
import shutil
import subprocess

import log
from app.utils.path_utils import PathUtils
from app.utils.exception_utils import ExceptionUtils
from app.utils.types import OsType
from app.utils.isolated_io import get_io_pool, IsolatedIOTimeout
from config import WEBDRIVER_PATH, Config

# rclone/mc 等外部转移工具会连接云端，网络异常或凭据失效时会永久挂起；而调用方
# 持有全局文件锁，因此一个挂起的进程会连带阻塞所有转移任务。合法的大文件传输
# 可能耗时很久，故默认取值放宽，并允许用 app.external_transfer_timeout 覆盖。
DEFAULT_EXTERNAL_TRANSFER_TIMEOUT = 3600


class SystemUtils:

    @staticmethod
    def get_external_transfer_timeout():
        """
        外部转移命令的超时（秒），可用 app.external_transfer_timeout 覆盖
        """
        try:
            configured = int(
                (Config().get_config("app") or {}).get("external_transfer_timeout") or 0
            )
        except (TypeError, ValueError):
            configured = 0
        return configured if configured > 0 else DEFAULT_EXTERNAL_TRANSFER_TIMEOUT

    @staticmethod
    def __run_external_transfer(command):
        """
        运行外部转移命令并施加超时
        :return: (returncode, message)；超时返回 -1，并提示目标可能残留不完整文件
        """
        timeout = SystemUtils.get_external_transfer_timeout()
        try:
            return get_io_pool('bulk').execute('external_command', command=command,
                                               timeout_seconds=timeout, timeout=timeout), ""
        except IsolatedIOTimeout:
            message = "外部转移命令超过 %s 秒未完成，已终止；目标位置可能残留不完整文件，请人工确认" \
                      % timeout
            log.error("【System】%s 超时" % " ".join(str(part) for part in command[:2]))
            return -1, message

    @staticmethod
    def __get_hidden_shell():
        if os.name == "nt":
            st = subprocess.STARTUPINFO()
            st.dwFlags = subprocess.STARTF_USESHOWWINDOW
            st.wShowWindow = subprocess.SW_HIDE
            return st
        else:
            return None

    @staticmethod
    def get_used_of_partition(path):
        """
        获取系统存储空间占用信息
        """
        if not path:
            return 0, 0
        if not os.path.exists(path):
            return 0, 0
        try:
            total_b, used_b, free_b = shutil.disk_usage(path)
            return used_b, total_b
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return 0, 0

    @staticmethod
    def get_system():
        """
        获取操作系统类型
        """
        if SystemUtils.is_windows():
            return OsType.WINDOWS
        elif SystemUtils.is_synology():
            return OsType.SYNOLOGY
        elif SystemUtils.is_docker():
            return OsType.DOCKER
        elif SystemUtils.is_macos():
            return OsType.MACOS
        else:
            return OsType.LINUX

    @staticmethod
    def get_free_space_gb(folder):
        """
        计算目录剩余空间大小
        """
        total_b, used_b, free_b = shutil.disk_usage(folder)
        return free_b / 1024 / 1024 / 1024

    @staticmethod
    def get_local_time(utc_time_str):
        """
        通过UTC的时间字符串获取时间
        """
        try:
            utc_date = datetime.datetime.strptime(utc_time_str.replace('0000', ''), '%Y-%m-%dT%H:%M:%S.%fZ')
            local_date = utc_date + datetime.timedelta(hours=8)
            local_date_str = datetime.datetime.strftime(local_date, '%Y-%m-%d %H:%M:%S')
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return utc_time_str
        return local_date_str

    @staticmethod
    def check_process(pname):
        """
        检查进程序是否存在
        """
        if not pname:
            return False
        text = subprocess.Popen('ps -ef | grep -v grep | grep %s' % pname, shell=True).communicate()
        return True if text else False

    @staticmethod
    def execute(cmd):
        """
        执行命令，获得返回结果
        """
        try:
            with os.popen(cmd) as p:
                return p.readline().strip()
        except Exception as err:
            print(str(err))
            return ""

    @staticmethod
    def is_docker():
        return os.path.exists('/.dockerenv')

    @staticmethod
    def is_synology():
        if SystemUtils.is_windows():
            return False
        return True if "synology" in SystemUtils.execute('uname -a') else False
        
    @staticmethod
    def is_windows():
        return True if os.name == "nt" else False

    @staticmethod
    def is_macos():
        return True if platform.system() == 'Darwin' else False

    @staticmethod
    def is_lite_version():
        return True if SystemUtils.is_docker() \
                       and os.environ.get("NASTOOL_VERSION") == "lite" else False

    @staticmethod
    def get_webdriver_path():
        if SystemUtils.is_lite_version():
            return None
        else:
            return WEBDRIVER_PATH.get(SystemUtils.get_system().value)

    @staticmethod
    def copy(src, dest):
        """
        复制
        """
        return SystemUtils._isolated_transfer(src, dest, 'copy')

    @staticmethod
    def move(src, dest):
        """
        移动
        """
        return SystemUtils._isolated_transfer(src, dest, 'move')

    @staticmethod
    def link(src, dest):
        """
        硬链接
        """
        return SystemUtils._isolated_transfer(src, dest, 'link')

    @staticmethod
    def softlink(src, dest):
        """
        软链接
        """
        return SystemUtils._isolated_transfer(src, dest, 'softlink')

    @staticmethod
    def _isolated_transfer(src, dest, mode):
        """Keep local bulk I/O out of application threads; retain uncertain outputs."""
        try:
            get_io_pool('bulk').execute('transfer', source=os.path.normpath(src),
                                        target=os.path.normpath(dest), mode=mode,
                                        timeout=SystemUtils.get_external_transfer_timeout())
            return 0, ""
        except IsolatedIOTimeout:
            # -2 means a mutation's result is uncertain, so protected publishers
            # must keep their owned staging inode for later reconciliation.
            return -2, '文件操作超过总时限，结果未确认；已保留源/暂存证据，请检查后重试'
        except Exception as err:
            ExceptionUtils.exception_traceback(err)
            return -1, str(err)

    @staticmethod
    def rclone_move(src, dest):
        """
        Rclone移动
        """
        try:
            src = os.path.normpath(src)
            dest = dest.replace("\\", "/")
            return SystemUtils.__run_external_transfer(
                ['rclone', 'moveto', src, f'NASTOOL:{dest}'])
        except Exception as err:
            ExceptionUtils.exception_traceback(err)
            return -1, str(err)

    @staticmethod
    def rclone_copy(src, dest):
        """
        Rclone复制
        """
        try:
            src = os.path.normpath(src)
            dest = dest.replace("\\", "/")
            return SystemUtils.__run_external_transfer(
                ['rclone', 'copyto', src, f'NASTOOL:{dest}'])
        except Exception as err:
            ExceptionUtils.exception_traceback(err)
            return -1, str(err)

    @staticmethod
    def minio_move(src, dest):
        """
        Minio移动
        """
        try:
            src = os.path.normpath(src)
            dest = dest.replace("\\", "/")
            if dest.startswith("/"):
                dest = dest[1:]
            return SystemUtils.__run_external_transfer(
                ['mc', 'mv', '--recursive', src, f'NASTOOL/{dest}'])
        except Exception as err:
            ExceptionUtils.exception_traceback(err)
            return -1, str(err)

    @staticmethod
    def minio_copy(src, dest):
        """
        Minio复制
        """
        try:
            src = os.path.normpath(src)
            dest = dest.replace("\\", "/")
            if dest.startswith("/"):
                dest = dest[1:]
            return SystemUtils.__run_external_transfer(
                ['mc', 'cp', '--recursive', src, f'NASTOOL/{dest}'])
        except Exception as err:
            ExceptionUtils.exception_traceback(err)
            return -1, str(err)

    @staticmethod
    def get_windows_drives():
        """
        获取Windows所有盘符
        """
        vols = []
        for i in range(65, 91):
            vol = chr(i) + ':'
            if os.path.isdir(vol):
                vols.append(vol)
        return vols

    def find_hardlinks(self, file, fdir=None):
        """
        查找文件的所有硬链接
        """
        ret_files = []
        if os.name == "nt":
            ret = subprocess.run(
                ['fsutil', 'hardlink', 'list', file],
                startupinfo=self.__get_hidden_shell(),
                stdout=subprocess.PIPE
            )
            if ret.returncode != 0:
                return []
            if ret.stdout:
                drive = os.path.splitdrive(file)[0]
                link_files = ret.stdout.decode('GBK').replace('\\', '/').split('\r\n')
                for link_file in link_files:
                    if link_file \
                            and "$RECYCLE.BIN" not in link_file \
                            and os.path.normpath(file) != os.path.normpath(f'{drive}{link_file}'):
                        link_file = f'{drive.upper()}{link_file}'
                        file_name = os.path.basename(link_file)
                        file_path = os.path.dirname(link_file)
                        ret_files.append({
                            "file": link_file,
                            "filename": file_name,
                            "filepath": file_path
                        })
        else:
            inode = os.stat(file).st_ino
            if not fdir:
                fdir = os.path.dirname(file)
            stdout = subprocess.run(
                ['find', fdir, '-inum', str(inode)],
                stdout=subprocess.PIPE
            ).stdout
            if stdout:
                link_files = stdout.decode('utf-8').split('\n')
                for link_file in link_files:
                    if link_file \
                            and os.path.normpath(file) != os.path.normpath(link_file):
                        file_name = os.path.basename(link_file)
                        file_path = os.path.dirname(link_file)
                        ret_files.append({
                            "file": link_file,
                            "filename": file_name,
                            "filepath": file_path
                        })

        return ret_files
