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
