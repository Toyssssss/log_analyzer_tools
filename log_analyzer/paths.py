import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile

WINDOWS = sys.platform == 'win32'

# Absolute-path prefix that lifts the MAX_PATH limit. Only ever applied to paths
# we do not construct ourselves (user-supplied archive, rmtree) -- it disables
# '.'/'..' normalisation, so it must stay scoped.
_LONG_PREFIX = '\\\\?\\'


def win_long(path: str) -> str:
    """Prefix \\\\?\\ on Windows for paths approaching MAX_PATH.

    Input must already be absolute. Returns the path unchanged when the prefix
    is unnecessary or unavailable (UNC paths need the \\\\?\\UNC\\ form, which we
    deliberately do not synthesise here -- we fall back to the plain path so the
    caller gets a normal, if length-limited, error instead of a wrong one.
    """
    if not WINDOWS or path.startswith(_LONG_PREFIX):
        return path
    path = os.path.abspath(path)
    if len(path) < 200:
        return path
    if path.startswith('\\\\'):
        return path
    return _LONG_PREFIX + path


def default_workspace_root() -> str:
    """Cross-platform workspace location."""
    if WINDOWS:
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
        return os.path.join(base, 'LogAnalyzer', 'workspace')
    xdg = os.environ.get('XDG_CACHE_HOME')
    base = xdg if xdg else os.path.join(os.path.expanduser('~'), '.cache')
    return os.path.join(base, 'log-analyzer')


def workspace_root(explicit: str = None) -> str:
    """Resolution order: explicit -> LOG_ANALYZER_WORKSPACE -> platform default."""
    root = explicit or os.environ.get('LOG_ANALYZER_WORKSPACE') or default_workspace_root()
    return os.path.abspath(os.path.expanduser(root))


def archive_key(archive_path: str) -> str:
    """Stable 16-char id for an archive identity (path + size + mtime).

    Re-selecting the same unchanged file is an instant cache hit; an edited file
    lands in a fresh directory and the old one becomes evictable.
    """
    real = os.path.realpath(archive_path)
    try:
        st = os.stat(real)
        size, mtime = st.st_size, st.st_mtime_ns
    except OSError:
        size, mtime = 0, 0
    blob = '%s|%d|%d' % (real, size, mtime)
    return hashlib.sha1(blob.encode('utf-8', 'surrogateescape')).hexdigest()[:16]


def archive_dir(ws_root: str, key: str) -> str:
    return os.path.join(ws_root, 'archives', key)


def data_path(ws_root: str, key: str, seq: int) -> str:
    """Sharded opaque on-disk name. Never derived from an archive member name,
    so it is always short and zip-slip is impossible by construction."""
    hh = (seq >> 12) & 0xff
    return os.path.join(ws_root, 'archives', key, 'data', '%02x' % hh, '%08x' % seq)


def ensure_dir(path: str) -> str:
    os.makedirs(win_long(path), exist_ok=True)
    return path


def atomic_write_bytes(path: str, data: bytes) -> None:
    """Write via temp file + fsync + os.replace. Atomic on NTFS and POSIX."""
    d = os.path.dirname(path)
    if d:
        ensure_dir(d)
    fd, tmp = tempfile.mkstemp(dir=d or '.', prefix='.tmp-', suffix='.part')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: str, obj) -> None:
    atomic_write_bytes(path, json.dumps(obj, ensure_ascii=False, indent=1).encode('utf-8'))


def read_json(path: str, default=None):
    try:
        with open(win_long(path), 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


_ONERROR_ERRNO = (13, 32)  # EACCES, ESHARED (Windows: file in use)


def rmtree_robust(path: str) -> None:
    """Remove a tree, clearing the read-only bit and retrying once.

    Unlike the old cleanup(), errors are NOT swallowed: reclaiming disk space is
    the whole point of this call, so a failure the user cannot see is worse than
    an exception the caller can report.
    """
    if not os.path.exists(win_long(path)):
        return

    def _onerror(func, p, exc_info):
        exc = exc_info[1] if isinstance(exc_info, tuple) else exc_info
        if getattr(exc, 'errno', None) in _ONERROR_ERRNO:
            try:
                os.chmod(p, stat.S_IWRITE)
                func(p)
                return
            except OSError:
                pass
        raise exc

    shutil.rmtree(win_long(path), onerror=_onerror)


def human_bytes(n: float) -> str:
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(n) < 1024.0 or unit == 'TB':
            return '%.0f %s' % (n, unit) if unit == 'B' else '%.1f %s' % (n, unit)
        n /= 1024.0


def free_space(path: str) -> int:
    """Free bytes on the volume holding path, or -1 if undeterminable."""
    probe = path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return -1
