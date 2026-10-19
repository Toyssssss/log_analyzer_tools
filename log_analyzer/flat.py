import hashlib
import os
from dataclasses import replace
from typing import Callable, List, Optional, Tuple

from .extractor import (ARCHIVE_KINDS, CancelToken, extract_archive,
                        scan_archive)
from .manifest import (FileEntry, Manifest, STATUS_COMPLETE, _utc_now,
                       clear_workspace, inspect_workspace)
from .naming import join_vp, repair_surrogates
from .paths import win_long
from .sniff import sniff_file

MAX_FLAT_ENTRIES = 300_000      # mirrors extractor.MAX_ENTRIES
MAX_WARNINGS = 20
CANCEL_EVERY = 2000
PROGRESS_EVERY = 5000

# Cache keys for archives found inside a folder are namespaced so they can
# never collide with the keys archive_key() derives from a file the user
# opened deliberately.
LOCAL_PREFIX = 'local-'


def walk_leaves(root: str, cancel: CancelToken = None,
                add_warning: Callable = None,
                progress: Callable = None) -> List[FileEntry]:
    """Every file under `root`, recursively, as root-relative FileEntries.

    Shared by both in-place scanning (nothing is copied) and the 'folder that
    contains archives' path (which turns each archive leaf into a cache key
    instead of an entry), so the two walkers can never disagree about what the
    tree contains.

    Deterministic: dirnames/filenames are sorted, so the same tree lists the
    same way on every run and across platforms. Symlink/junction loops are
    pruned by (st_dev, st_ino) identity, falling back to realpath when the
    filesystem reports no inode.
    """
    cancel = cancel or CancelToken()
    warn = add_warning or (lambda msg: None)
    progress = progress or (lambda *a: None)
    entries: List[FileEntry] = []
    visited = set()
    root_abs = os.path.abspath(root)
    next_id = 0

    def on_error(err: OSError) -> None:
        warn('%s: %s' % (getattr(err, 'filename', '?'), err))

    try:
        walker = os.walk(root_abs, topdown=True, onerror=on_error,
                         followlinks=False)
        for dirpath, dirnames, filenames in walker:
            cancel.raise_if_cancelled()
            if not _mark_visited(visited, dirpath):
                dirnames[:] = []
                warn('skipping a directory loop at %s' % dirpath)
                continue
            dirnames.sort()
            filenames.sort()
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                try:
                    size = os.stat(full).st_size
                except OSError as e:
                    warn('cannot stat %s: %s' % (full, e))
                    continue
                rel = _relative(root_abs, full)
                entries.append(FileEntry(
                    id=next_id, vp=repair_surrogates(rel),
                    disk=os.path.relpath(full, root_abs), size=size, depth=0))
                next_id += 1
                if len(entries) >= MAX_FLAT_ENTRIES:
                    warn('stopped after %d files: the folder is larger than '
                         'this tool will list at once' % MAX_FLAT_ENTRIES)
                    return entries
                if next_id % CANCEL_EVERY == 0:
                    cancel.raise_if_cancelled()
                if progress and next_id % PROGRESS_EVERY == 0:
                    progress('scan', next_id, 0, rel)
    except OSError as e:
        # os.walk reports a failed readdir through onerror, but a root that
        # cannot be entered at all can surface here too. Either way this is a
        # listing the user asked for and cannot have: a warning, not a crash.
        warn('%s: %s' % (root_abs, e))
    return entries


def _relative(root: str, path: str) -> str:
    """Path relative to the chosen root, with '/' separators.

    Forward slashes keep FileEntry.vp readable and, more importantly, keep
    FileEntry.name (which splits on '/') working on Windows.
    """
    return os.path.relpath(path, root).replace(os.sep, '/')


def scan_flat(root: str, cancel: CancelToken = None,
              progress: Callable = None,
              ws_root: str = None) -> Manifest:
    """Enumerate a folder or a single file for in-place searching.

    Nothing is written anywhere: the only filesystem calls are stat/readdir, and
    the returned Manifest is never persisted. Entries carry a root-relative
    `disk`, so `root + disk` addresses the real file -- which is exactly what
    FlatSource relies on.

    Unreadable subdirectories are collected as warnings rather than raising,
    so one locked folder does not abandon a whole scan.
    """
    root = os.path.abspath(root)
    cancel = cancel or CancelToken()
    progress = progress or (lambda *a: None)
    warnings: List[str] = []
    warnings_dropped = [0]

    def add_warning(msg: str) -> None:
        if len(warnings) < MAX_WARNINGS:
            warnings.append(msg)
        else:
            warnings_dropped[0] += 1

    if os.path.isfile(root):
        # A single file is a one-entry scope; its base is the parent directory.
        try:
            size = os.stat(root).st_size
        except OSError as e:
            add_warning('cannot stat %s: %s' % (root, e))
            size = 0
        name = repair_surrogates(os.path.basename(root))
        entries = [FileEntry(id=0, vp=name, disk=name, size=size, depth=0)]
    else:
        # A path that is not a file is treated as a folder even when it is not
        # there: os.walk reports the failure through onerror and the scan comes
        # back empty with a warning, which is more useful than an exception.
        if _contains_workspace(root, ws_root):
            add_warning('this tree contains the extraction workspace: %s' % root)
        entries = walk_leaves(root, cancel=cancel, add_warning=add_warning,
                              progress=progress)

    if warnings_dropped[0]:
        warnings.append('... and %d more' % warnings_dropped[0])

    return Manifest(
        status=STATUS_COMPLETE,
        archive_name=root,
        archive_path=root,
        stats={
            'archives': 0,
            'files': len(entries),
            'total_bytes': sum(e.size for e in entries),
            'max_depth': 0,
            'top_level_entries': len(entries),
        },
        created_utc=_utc_now(),
        completed_utc=_utc_now(),
        warnings=warnings,
        entries=entries,
    )


def scan_tree(root: str, ws_root: str, cancel: CancelToken = None,
              progress: Callable = None) -> Tuple[Manifest, tuple]:
    """One folder scope covering both its loose files and its archives.

    The whole tree is walked first. Loose files are read where they are; every
    archive found anywhere in it is extracted into the workspace so that its
    *contents* are what gets searched, instead of the archive's own compressed
    bytes. One scope, one search: the caller sees a single entry list with no
    idea that two kinds of storage are behind it.

    Returns (manifest, local_keys) -- the cache keys this pass created, which
    the caller needs in order to clear them later.

    Archives already in the cache from a previous run of this same folder reuse
    it when the file is unchanged, and are wiped and redone when it is not, so
    the results track the folder rather than going stale.
    """
    root = os.path.abspath(root)
    cancel = cancel or CancelToken()
    progress = progress or (lambda *a: None)
    warnings: List[str] = []
    warnings_dropped = [0]

    def add_warning(msg: str) -> None:
        if len(warnings) < MAX_WARNINGS:
            warnings.append(msg)
        else:
            warnings_dropped[0] += 1

    if _contains_workspace(root, ws_root):
        add_warning('this tree contains the extraction workspace: %s' % root)

    leaves = walk_leaves(root, cancel=cancel, add_warning=add_warning)
    archives = []
    entries: List[FileEntry] = []
    for e in leaves:
        cancel.raise_if_cancelled()
        if _is_archive(os.path.join(root, e.disk)):
            archives.append(e)
        else:
            entries.append(e)

    manifest = Manifest(
        status=STATUS_COMPLETE, archive_name=root, archive_path=root,
        created_utc=_utc_now(), completed_utc=_utc_now(),
        stats={'archives': len(archives), 'files': len(entries),
               'total_bytes': sum(e.size for e in entries), 'max_depth': 0,
               'top_level_entries': len(leaves)},
        warnings=[], entries=entries)

    if not archives:
        manifest.warnings = warnings
        return manifest, ()

    # One folder of archives maps onto one workspace-relative directory, so
    # re-picking the folder hits the cache and re-picking it after deleting the
    # data self-heals instead of stranding orphans under the plain keys.
    folder_key = _local_key(root)
    index_origin = inspect_workspace(ws_root, folder_key)[0]
    # A leftover 'partial' is always a dead run: a completed pass overwrites it.
    if index_origin in ('partial', 'failed'):
        clear_workspace(ws_root, folder_key)

    n = len(archives)
    local_keys = []
    for i, e in enumerate(archives, 1):
        cancel.raise_if_cancelled()
        src = os.path.join(root, e.disk)
        key = _local_key(src)
        if key not in local_keys:
            local_keys.append(key)
        progress('scan', i, n, e.vp)
        man = _extract_local(src, ws_root, key, cancel, i, n, progress)
        if man is None:
            add_warning('cannot extract %s' % e.vp)
            continue
        if man.status != STATUS_COMPLETE:
            add_warning('only part of %s could be extracted' % e.vp)
        # Archive contents are appended after the loose files, each carrying
        # the key of the cache directory it landed in. vp gains the archive's
        # own path so two same-named members in different archives never
        # collide, and so the result tree still says where a file came from.
        # extract_archive already prefixes every vp with the archive's own
        # basename, which is the tail of e.vp -- strip it so the name does not
        # appear twice ('sub/b.zip/b.zip/x.log' -> 'sub/b.zip/x.log').
        stem = os.path.basename(src) + '/'
        for le in man.entries:
            inner = le.vp[len(stem):] if le.vp.startswith(stem) else le.vp
            entries.append(replace(le, vp=join_vp(e.vp, inner), key=key))
        manifest.stats['max_depth'] = max(manifest.stats['max_depth'],
                                          max((x.depth + 1 for x in man.entries),
                                              default=0))
        if man.warnings:
            add_warning('%s: %s' % (e.vp, man.warnings[0]))

    if warnings_dropped[0]:
        warnings.append('... and %d more' % warnings_dropped[0])
    manifest.warnings = warnings
    manifest.stats['files'] = len(entries)
    manifest.stats['total_bytes'] = sum(x.size for x in entries)
    manifest.stats['top_level_entries'] = max(len(leaves), 1)
    return manifest, tuple(local_keys)


def _is_archive(path: str) -> bool:
    """Magic-byte sniff, not the extension.

    A log file that happens to end in .zip is far less common than an archive
    that does not end in one, and the sniff is what the rest of the tool
    already trusts.
    """
    try:
        return sniff_file(win_long(path)) in ARCHIVE_KINDS
    except OSError:
        return False


def _local_key(path: str) -> str:
    """Cache key for one file, namespaced away from user-picked archives.

    Content-based (path + size + mtime), so a growing log directory reuses the
    cache for every archive it did not touch.
    """
    real = os.path.realpath(path)
    try:
        st = os.stat(win_long(real))
        size, mtime = st.st_size, st.st_mtime_ns
    except OSError:
        size, mtime = 0, 0
    blob = '%s|%d|%d' % (real, size, mtime)
    digest = hashlib.sha1(blob.encode('utf-8', 'surrogateescape')).hexdigest()[:16]
    return LOCAL_PREFIX + digest


def _extract_local(path: str, ws_root: str, key: str, cancel: CancelToken,
                   i: int, n: int, progress: Callable):
    """Extract one archive found in a folder, reusing its cache if intact.

    extract_archive already returns the cached manifest when the key exists and
    is complete, so an unchanged folder is a pure cache hit. The root-level cap
    on total extracted bytes is deliberately left off: the user asked for this
    folder in full, and silently dropping archives would be a false negative.
    """
    try:
        structural = scan_archive(path, cancel)
        return extract_archive(
            path, ws_root, key, cancel=cancel, structural=structural,
            log=lambda m: None,
            progress=lambda ph, d, t, note: progress('scan', i, n, note))
    except ValueError:
        # Magic bytes said archive but the container is truncated, corrupt or
        # unsupported. A bad leaf must not abandon the folder it sits in --
        # the file is simply skipped, with one warning naming it.
        return None


def _mark_visited(visited: set, path: str) -> bool:
    """False when this directory was already walked (a loop)."""
    try:
        st = os.stat(path)
        ident = (st.st_dev, st.st_ino)
        if not st.st_ino:
            ident = ('realpath', os.path.realpath(path))
    except OSError:
        return True
    if ident in visited:
        return False
    visited.add(ident)
    return True


def _contains_workspace(dirpath: str, ws_root: str) -> bool:
    if not ws_root:
        return False
    try:
        inner = os.path.normcase(os.path.realpath(dirpath))
        target = os.path.normcase(os.path.realpath(ws_root))
    except OSError:
        return False
    return target == inner or target.startswith(inner + os.sep)
