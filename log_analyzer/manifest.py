import json
import os
import time
from dataclasses import dataclass, field, asdict
from typing import List, Optional

from .paths import (
    atomic_write_json, archive_dir, data_path, ensure_dir, human_bytes,
    read_json, rmtree_robust, win_long,
)

SCHEMA = 1
MANIFEST_NAME = 'manifest.json'
PARTIAL_NAME = 'manifest.partial.jsonl'
MARKER_NAME = 'EXTRACTING'
INDEX_NAME = 'index.json'

STATUS_COMPLETE = 'complete'
STATUS_PARTIAL = 'partial'
STATUS_FAILED = 'failed'


def _utc_now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


@dataclass
class FileEntry:
    """One leaf file, i.e. something that is not itself an archive."""
    id: int
    vp: str                     # human-readable path inside the archive chain
    disk: str                   # path relative to archives/<key>/
    size: int
    depth: int                  # archive layers traversed to reach this file
    parent: Optional[str] = None   # vp of the immediately enclosing archive
    binary: bool = False

    @property
    def name(self) -> str:
        return self.vp.rsplit('/', 1)[-1]


@dataclass
class ScanSummary:
    """Preflight result: structure only, no member decompressed."""
    archives: int = 0
    files: int = 0
    total_bytes: int = 0
    max_depth: int = 0
    top_level_entries: int = 0
    warnings: List[str] = field(default_factory=list)

    def describe(self) -> str:
        return '%d archives, %d files, %s' % (
            self.archives, self.files, human_bytes(self.total_bytes))


@dataclass
class Manifest:
    schema: int = SCHEMA
    status: str = STATUS_COMPLETE
    archive_name: str = ''
    archive_path: str = ''
    archive_size: int = 0
    archive_mtime_ns: int = 0
    created_utc: str = ''
    completed_utc: str = ''
    stats: dict = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    entries: List[FileEntry] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return self.status == STATUS_COMPLETE

    @property
    def total_bytes(self) -> int:
        return int(self.stats.get('total_bytes', 0))

    def to_json(self) -> dict:
        d = asdict(self)
        # Flatten to the documented schema shape.
        return {
            'schema': d['schema'],
            'status': d['status'],
            'archive': {
                'display_name': d['archive_name'],
                'path': d['archive_path'],
                'size': d['archive_size'],
                'mtime_ns': d['archive_mtime_ns'],
            },
            'created_utc': d['created_utc'],
            'completed_utc': d['completed_utc'],
            'stats': d['stats'],
            'warnings': d['warnings'],
            'entries': [
                {'id': e['id'], 'vp': e['vp'], 'disk': e['disk'], 'size': e['size'],
                 'depth': e['depth'], 'parent': e['parent'], 'binary': e['binary']}
                for e in d['entries']
            ],
        }

    @classmethod
    def from_json(cls, obj: dict) -> 'Manifest':
        a = obj.get('archive', {})
        m = cls(
            schema=obj.get('schema', SCHEMA),
            status=obj.get('status', STATUS_COMPLETE),
            archive_name=a.get('display_name', ''),
            archive_path=a.get('path', ''),
            archive_size=a.get('size', 0),
            archive_mtime_ns=a.get('mtime_ns', 0),
            created_utc=obj.get('created_utc', ''),
            completed_utc=obj.get('completed_utc', ''),
            stats=obj.get('stats', {}),
            warnings=obj.get('warnings', []),
        )
        for e in obj.get('entries', []):
            m.entries.append(FileEntry(
                id=e.get('id', 0), vp=e.get('vp', ''), disk=e.get('disk', ''),
                size=e.get('size', 0), depth=e.get('depth', 0),
                parent=e.get('parent'), binary=e.get('binary', False)))
        return m


# ---------------------------------------------------------------- manifest IO

def manifest_path(ws_root: str, key: str) -> str:
    return os.path.join(archive_dir(ws_root, key), MANIFEST_NAME)


def partial_path(ws_root: str, key: str) -> str:
    return os.path.join(archive_dir(ws_root, key), PARTIAL_NAME)


def marker_path(ws_root: str, key: str) -> str:
    return os.path.join(archive_dir(ws_root, key), MARKER_NAME)


def write_manifest(ws_root: str, key: str, man: Manifest) -> None:
    """Atomic finalize. Written only at a terminal state."""
    man.completed_utc = man.completed_utc or _utc_now()
    atomic_write_json(manifest_path(ws_root, key), man.to_json())
    for p in (partial_path(ws_root, key), marker_path(ws_root, key)):
        try:
            os.unlink(win_long(p))
        except OSError:
            pass


def read_manifest(ws_root: str, key: str) -> Optional[Manifest]:
    obj = read_json(manifest_path(ws_root, key))
    if not isinstance(obj, dict):
        return None
    return Manifest.from_json(obj)


def begin_extraction(ws_root: str, key: str) -> None:
    """Create the in-progress marker BEFORE the first byte is written."""
    atomic_write_json(marker_path(ws_root, key), {
        'pid': os.getpid(), 'started_utc': _utc_now()})


def append_partial(ws_root: str, key: str, entries: List[FileEntry],
                   top_index: int) -> None:
    """Append one JSON line per completed top-level entry, fsynced.

    A torn final line from a power loss fails json.loads on read and is simply
    discarded, so the file is self-healing for its exact failure mode.
    """
    ensure_dir(archive_dir(ws_root, key))
    rec = {
        'top_index': top_index,
        'entries': [
            {'id': e.id, 'vp': e.vp, 'disk': e.disk, 'size': e.size,
             'depth': e.depth, 'parent': e.parent, 'binary': e.binary}
            for e in entries
        ],
    }
    line = json.dumps(rec, ensure_ascii=False) + '\n'
    with open(win_long(partial_path(ws_root, key)), 'a', encoding='utf-8') as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def read_partial(ws_root: str, key: str):
    """Recover completed top-level entries from an interrupted run.

    Returns (entries, completed_top_indices, skipped_lines).
    """
    p = partial_path(ws_root, key)
    entries: List[FileEntry] = []
    done = set()
    bad = 0
    if not os.path.exists(win_long(p)):
        return entries, done, bad
    with open(win_long(p), 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                bad += 1          # torn tail or corrupt line; discard it
                continue
            done.add(rec.get('top_index'))
            for e in rec.get('entries', []):
                entries.append(FileEntry(
                    id=e.get('id', 0), vp=e.get('vp', ''), disk=e.get('disk', ''),
                    size=e.get('size', 0), depth=e.get('depth', 0),
                    parent=e.get('parent'), binary=e.get('binary', False)))
    return entries, done, bad


def inspect_workspace(ws_root: str, key: str):
    """Classify what is on disk for this archive.

    Returns (state, manifest_or_None, partial_entries, completed_top_indices).
    state is one of 'absent', 'complete', 'partial'.

    The states are kept mutually exclusive so a partial extraction can never be
    mistaken for a complete one: search is only allowed against 'complete', or
    against 'partial' after explicit user consent.
    """
    man = read_manifest(ws_root, key)
    marker = os.path.exists(win_long(marker_path(ws_root, key)))
    if man is not None:
        if marker:
            # The atomic replace succeeded but cleanup was interrupted.
            try:
                os.unlink(win_long(marker_path(ws_root, key)))
            except OSError:
                pass
        return 'complete', man, [], set()
    entries, done, _bad = read_partial(ws_root, key)
    if marker or entries:
        return 'partial', None, entries, done
    return 'absent', None, [], set()


def build_partial_manifest(ws_root: str, key: str, archive_path: str,
                           entries: List[FileEntry], done: set,
                           structural: ScanSummary) -> Manifest:
    """Assemble a searchable manifest from an interrupted run's JSONL."""
    try:
        st = os.stat(archive_path)
        size, mtime = st.st_size, st.st_mtime_ns
    except OSError:
        size, mtime = 0, 0
    total = sum(e.size for e in entries)
    return Manifest(
        status=STATUS_PARTIAL,
        archive_name=os.path.basename(archive_path),
        archive_path=os.path.abspath(archive_path),
        archive_size=size,
        archive_mtime_ns=mtime,
        created_utc=_utc_now(),
        stats={
            'archives': structural.archives,
            'files': len(entries),
            'total_bytes': total,
            'max_depth': max((e.depth for e in entries), default=0),
            'top_level_entries': structural.top_level_entries,
            'completed_entries': len(done),
        },
        warnings=['recovered from an interrupted extraction'],
        entries=entries,
    )


# ------------------------------------------------------------------- index IO

def index_path(ws_root: str) -> str:
    return os.path.join(ws_root, INDEX_NAME)


def load_index(ws_root: str) -> dict:
    obj = read_json(index_path(ws_root))
    if not isinstance(obj, dict) or 'archives' not in obj:
        return {'schema': SCHEMA, 'archives': []}
    return obj


def index_upsert(ws_root: str, ref: dict) -> None:
    idx = load_index(ws_root)
    rest = [a for a in idx['archives'] if a.get('key') != ref.get('key')]
    rest.append(ref)
    idx['archives'] = rest
    atomic_write_json(index_path(ws_root), idx)


def index_remove(ws_root: str, key: str) -> None:
    idx = load_index(ws_root)
    idx['archives'] = [a for a in idx['archives'] if a.get('key') != key]
    atomic_write_json(index_path(ws_root), idx)


def index_touch(ws_root: str, key: str) -> None:
    idx = load_index(ws_root)
    for a in idx['archives']:
        if a.get('key') == key:
            a['last_used_utc'] = _utc_now()
            atomic_write_json(index_path(ws_root), idx)
            return


def make_ref(key: str, man: Manifest) -> dict:
    return {
        'key': key,
        'display_name': man.archive_name,
        'path': man.archive_path,
        'status': man.status,
        'files': len(man.entries),
        'bytes': man.total_bytes,
        'last_used_utc': _utc_now(),
    }


def scan_workspace(ws_root: str) -> dict:
    """Reconcile index.json against what is actually on disk.

    Drops index rows whose directory is gone and counts real usage, so the
    workspace size label never lies to the user.
    """
    idx = load_index(ws_root)
    alive = []
    total = 0
    for a in idx['archives']:
        key = a.get('key')
        if not key:
            continue
        d = archive_dir(ws_root, key)
        if not os.path.isdir(d):
            continue
        size = _dir_size(d)
        a['bytes'] = size
        total += size
        alive.append(a)
    if len(alive) != len(idx['archives']):
        idx['archives'] = alive
        atomic_write_json(index_path(ws_root), idx)
    return {'archives': alive, 'total_bytes': total}


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def clear_workspace(ws_root: str, key: str = None) -> None:
    """Delete one archive's data, or everything. Raises on real failures."""
    if key:
        rmtree_robust(archive_dir(ws_root, key))
        index_remove(ws_root, key)
    else:
        rmtree_robust(os.path.join(ws_root, 'archives'))
        atomic_write_json(index_path(ws_root), {'schema': SCHEMA, 'archives': []})
