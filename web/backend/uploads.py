"""Private upload staging shared by HTTP intake and its action consumers."""
import ntpath
import os
import tempfile

from werkzeug.utils import secure_filename


def upload_name(value, extension):
    """Reject path syntax on both platforms before accepting a filename."""
    if (not isinstance(value, str) or not value or '\x00' in value
            or '/' in value or '\\' in value or ':' in value
            or ntpath.basename(value) != value or value in ('.', '..')
            or not value.lower().endswith(extension)):
        raise ValueError('上传文件名或类型无效')
    return value


def upload_directory(temp_path, kind, owner):
    """Separate backup/torrent storage and each user's torrent capability."""
    import hashlib
    user_key = hashlib.sha256(str(owner).encode()).hexdigest()
    return os.path.join(temp_path, 'uploads', kind, user_key)


def save_upload(file, directory, extension):
    """Allocate a fresh inode: collisions or existing links can never be overwritten."""
    name = upload_name(file.filename, extension)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    safe = secure_filename(name)
    if not safe.lower().endswith(extension):
        safe = 'upload' + extension
    fd, path = tempfile.mkstemp(prefix='upload-', suffix='-' + safe, dir=directory)
    try:
        with os.fdopen(fd, 'wb') as output:
            file.save(output)
    except BaseException:
        os.unlink(path)
        raise
    return {'code': 0, 'filename': os.path.basename(path), 'filepath': path}


def uploaded_path(directory, name, extension):
    """Actions revalidate input independently of the browser/upload endpoint."""
    path = os.path.join(directory, upload_name(name, extension))
    if os.path.islink(path) or not os.path.isfile(path):
        raise ValueError('上传文件不存在或路径无效')
    return path
