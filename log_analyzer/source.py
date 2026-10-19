import os

from .extractor import CancelToken, open_archive
from .paths import win_long
from .sniff import sniff, sniff_file

CHUNK = 1 << 20


class DiskSource:
    """Reads leaf files from the materialised workspace."""

    def __init__(self, ws_root: str, key: str):
        self.base = os.path.join(ws_root, 'archives', key)

    def path_for(self, entry) -> str:
        return os.path.join(self.base, *entry.disk.split('/'))

    def open(self, entry):
        return open(win_long(self.path_for(entry)), 'rb')


class FlatSource:
    """Reads leaves straight from a folder or a single file. Nothing copied.

    `base` is the directory the entries' `disk` values are relative to: the
    chosen folder, or the chosen file's parent directory when the scope is one
    file. The two sources are interchangeable to the search core, which only
    ever calls open(entry).
    """

    def __init__(self, base: str):
        self.base = os.path.abspath(base)

    def path_for(self, entry) -> str:
        return os.path.join(self.base, *entry.disk.split(os.sep))

    def open(self, entry):
        return open(win_long(self.path_for(entry)), 'rb')


class HybridSource:
    """Dispatches each entry to the source that actually holds it.

    A folder scope can legitimately contain both files sitting in the folder and
    files that came out of an archive found inside it, which lives in the
    cache instead. FileEntry.key names that cache directory; '' (the default
    every other scope uses) means 'read it in place'.

    Wrapping is skipped entirely for a plain archive scope, so the common path
    keeps a single source and no indirection.
    """

    def __init__(self, primary, ws_root: str = None):
        self.primary = primary
        self.ws_root = ws_root
        self._keys = {}

    def _source_for(self, entry):
        key = getattr(entry, 'key', '') or ''
        if not key or not self.ws_root:
            return self.primary
        src = self._keys.get(key)
        if src is None:
            src = DiskSource(self.ws_root, key)
            self._keys[key] = src
        return src

    def path_for(self, entry) -> str:
        return self._source_for(entry).path_for(entry)

    def open(self, entry):
        return self._source_for(entry).open(entry)


class ZipChainSource:
    """Reads leaves by walking the archive chain in memory. Nothing on disk.

    Reserved for a future streaming mode; the search core is written against
    this protocol so switching modes is a construction change, not a redesign.
    """

    def __init__(self, archive_path: str):
        self.archive_path = archive_path
        self._root = None
        self._root_kind = None

    def _open_root(self):
        if self._root is None:
            kind = sniff_file(win_long(self.archive_path))
            self._root_kind = kind
            self._root = open_archive(self.archive_path, kind)
        return self._root

    def open(self, entry):
        raise NotImplementedError('streaming source is not enabled in this build')

    def close(self):
        if self._root is not None:
            try:
                self._root.close()
            except Exception:
                pass
            self._root = None
