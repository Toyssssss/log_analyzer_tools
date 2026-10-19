import os
import tempfile
from typing import Optional, Tuple

from .paths import (
    atomic_write_json, default_config_dir, ensure_dir, read_json, win_long,
    workspace_root,
)

SETTINGS_NAME = 'settings.json'
ENV_VAR = 'LOG_ANALYZER_WORKSPACE'
VERSION = 1


def settings_path() -> str:
    return os.path.join(default_config_dir(), SETTINGS_NAME)


def load_settings() -> dict:
    """The preferences file, or {} when it is missing, torn or not an object.

    read_json already swallows OSError and ValueError, so a half-written file
    (power loss during the atomic replace) is a non-event rather than a crash
    on startup.
    """
    obj = read_json(settings_path())
    return obj if isinstance(obj, dict) else {}


def saved_workspace() -> Optional[str]:
    """The persisted workspace, or None when unset or not absolute.

    A relative value is ignored rather than resolved: it would silently point
    at a different place depending on the working directory the app happened
    to be launched from.
    """
    value = load_settings().get('workspace')
    if not isinstance(value, str) or not value.strip():
        return None
    if not os.path.isabs(os.path.expanduser(value)):
        return None
    return workspace_root(value)


def save_workspace(path: str) -> bool:
    """Persist the workspace choice. Returns False instead of raising.

    Read-modify-write so unrelated keys (a future 'last_keyword') survive, and
    a failure to persist must never block the user from working in the new
    location for this session.
    """
    obj = load_settings()
    obj['version'] = obj.get('version', VERSION)
    obj['workspace'] = path
    try:
        atomic_write_json(settings_path(), obj)
        return True
    except OSError:
        return False


def resolve_workspace(explicit: str = None) -> Tuple[str, str]:
    """Resolve the workspace and say where the answer came from.

    Order: explicit -> LOG_ANALYZER_WORKSPACE -> settings.json -> default.
    The env var deliberately outranks the saved preference: it is the
    documented override for locked-down or quota-limited machines, and a stale
    settings file must not quietly defeat it. The GUI pays that back by showing
    the origin next to the path.
    """
    env = os.environ.get(ENV_VAR)
    saved = saved_workspace()
    if explicit:
        return workspace_root(explicit), 'cli'
    if env:
        return workspace_root(env), 'env'
    if saved:
        return saved, 'settings'
    return workspace_root(None), 'default'


def workspace_is_usable(path: str) -> Tuple[bool, str]:
    """(ok, reason) for a candidate workspace directory.

    The probe is a real create-and-delete rather than os.access(W_OK), which
    reports writable for directories that Windows ACLs will refuse. Free space
    is deliberately not checked here: the extraction preflight already warns.
    """
    try:
        ensure_dir(path)
    except OSError as e:
        return False, 'cannot create the folder: %s' % e
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=win_long(path), prefix='.write-probe-')
        os.close(fd)
        return True, ''
    except OSError as e:
        return False, 'the folder is not writable: %s' % e
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
