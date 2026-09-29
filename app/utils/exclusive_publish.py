"""Same-volume publication without replacing a concurrent writer's destination."""
import ctypes
import errno
import os
import sys


def rename_exclusive(source, destination):
    """Use native no-replace rename; never emulate it with exists() plus rename()."""
    if os.name == 'nt':
        # Windows rename refuses existing destinations, unlike POSIX rename.
        os.rename(source, destination)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin' and hasattr(libc, 'renamex_np'):
        fn = libc.renamex_np
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        args = (os.fsencode(source), os.fsencode(destination), 4)  # RENAME_EXCL (sys/stdio.h)
    elif sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
        fn = libc.renameat2
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        args = (-100, os.fsencode(source), -100, os.fsencode(destination), 1)  # AT_FDCWD, RENAME_NOREPLACE
    else:
        raise OSError(errno.ENOTSUP, '当前平台不支持排他重命名，保留源文件', destination)
    fn.restype = ctypes.c_int
    if fn(*args) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), destination)
