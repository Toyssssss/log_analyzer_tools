import bz2
import gzip
import io
import lzma
import os
import tarfile
import threading
import zipfile
from typing import Callable, List, Optional

from .manifest import (
    FileEntry, Manifest, ScanSummary, _utc_now, append_partial,
    begin_extraction, build_partial_manifest, inspect_workspace, write_manifest,
)
from .naming import decode_entry_name, join_vp, repair_surrogates
from .paths import data_path, ensure_dir, human_bytes, rmtree_robust, win_long
from .sniff import looks_like_archive_name, sniff, sniff_file

CHUNK = 1 << 20
HEAD_LEN = 512

MAX_DEPTH = 8               # measured max is 3; also the cycle guard
MAX_ENTRIES = 300_000       # measured 6,674
MAX_TOTAL_BYTES = 20 << 30
MAX_SINGLE_FILE = 2 << 30
MAX_RATIO = 1000            # only checked when compress_size > 0
MIN_RATIO_CHECK_SIZE = 1 << 20
MAX_SCAN_MEMBER = 512 << 20  # cap on a nested archive held in RAM during scan

ARCHIVE_KINDS = ('zip', 'tar', 'gzip', 'bzip2', 'xz')


class Cancelled(Exception):
    pass


class CancelToken:
    """Cooperative cancellation, shared between the worker and the UI."""

    __slots__ = ('_event',)

    def __init__(self):
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise Cancelled()


class _Member:
    """A single entry inside an archive, behind a uniform read interface."""

    __slots__ = ('name', 'size', 'compress_size', '_open')

    def __init__(self, name, size, compress_size, open_fn):
        self.name = name
        self.size = size
        self.compress_size = compress_size
        self._open = open_fn

    def open(self):
        return self._open()

    @property
    def looks_like_archive(self) -> bool:
        return looks_like_archive_name(self.name)


def _strip_single_ext(name: str, kind: str) -> str:
    suffix = {'gzip': '.gz', 'bzip2': '.bz2', 'xz': '.xz'}[kind]
    return name[:-len(suffix)] if name.lower().endswith(suffix) else name + '.out'


def open_archive(path: str, kind: str):
    p = win_long(path)
    if kind == 'zip':
        return zipfile.ZipFile(p, 'r')
    if kind == 'tar':
        return tarfile.open(p, 'r:*')
    if kind == 'gzip':
        return gzip.open(p, 'rb')
    if kind == 'bzip2':
        return bz2.open(p, 'rb')
    if kind == 'xz':
        return lzma.open(p, 'rb')
    raise ValueError('unsupported archive kind: %r' % kind)


def _open_by_bytes(data: bytes, kind: str):
    bio = io.BytesIO(data)
    if kind == 'zip':
        return zipfile.ZipFile(bio, 'r')
    if kind == 'tar':
        return tarfile.open(fileobj=bio, mode='r:*')
    if kind == 'gzip':
        return gzip.open(bio, 'rb')
    if kind == 'bzip2':
        return bz2.open(bio, 'rb')
    if kind == 'xz':
        return lzma.open(bio, 'rb')
    raise ValueError('unsupported archive kind: %r' % kind)


def _members(handle, kind: str, source_name: str):
    """Yield _Member for every non-directory entry. Occupies the handle."""
    if kind == 'zip':
        for info in handle.infolist():
            if info.is_dir():
                continue
            yield _Member(
                decode_entry_name(info), info.file_size, info.compress_size,
                lambda i=info: handle.open(i, 'r'))
    elif kind == 'tar':
        for m in handle.getmembers():
            if not m.isfile():
                continue
            yield _Member(
                repair_surrogates(m.name), m.size, 0,
                lambda mm=m: handle.extractfile(mm))
    else:
        # Single-stream compression: one synthetic member, readable once.
        yield _Member(_strip_single_ext(source_name, kind), -1, 0, lambda: handle)


def _check_ratio(mem: _Member, log: Callable) -> bool:
    """False when the member looks like a decompression bomb."""
    if not mem.compress_size or mem.size <= MIN_RATIO_CHECK_SIZE:
        return True
    if mem.size / float(mem.compress_size) > MAX_RATIO:
        log('skipping %s: compression ratio %d:1 exceeds %d:1' % (
            mem.name, mem.size // mem.compress_size, MAX_RATIO))
        return False
    return True


def _peek_kind(mem: _Member) -> Optional[str]:
    """Sniff a member's first bytes without consuming it for later use."""
    try:
        src = mem.open()
    except Exception:
        return None
    if src is None:
        return None
    try:
        return sniff(src.read(HEAD_LEN))
    except Exception:
        return None
    finally:
        try:
            src.close()
        except Exception:
            pass


# ----------------------------------------------------------------------- scan

def scan_archive(archive_path: str, cancel: Optional[CancelToken] = None) -> ScanSummary:
    """Walk nested central directories only, decompressing at most HEAD_LEN per
    leaf. Produces the preflight estimate AND the exact progress denominator.

    Nested archives are read into memory one at a time; peak memory is the
    largest single nested archive, not the tree.
    """
    path = win_long(archive_path)
    kind = sniff_file(path)
    if kind not in ARCHIVE_KINDS:
        raise ValueError('not a recognised archive (magic: %s)' % (kind or 'unknown'))

    summary = ScanSummary()
    seen_tops = [0]

    def walk(handle, hkind, source_name, depth):
        try:
            for mem in _members(handle, hkind, source_name):
                if cancel:
                    cancel.raise_if_cancelled()
                if depth == 0:
                    seen_tops[0] += 1
                # The ratio guard applies to every member, not just nested
                # archives: a leaf bomb is the same hazard, and real log files
                # do not compress 1000:1.
                if not _check_ratio(mem, summary.warnings.append):
                    continue
                nkind = _peek_kind(mem) if depth < MAX_DEPTH else None
                if nkind in ARCHIVE_KINDS:
                    if mem.size > MAX_SCAN_MEMBER:
                        summary.warnings.append(
                            'nested archive %s is %s, over the %s scan cap' % (
                                mem.name, human_bytes(mem.size),
                                human_bytes(MAX_SCAN_MEMBER)))
                        summary.files += 1
                        continue
                    try:
                        src = mem.open()
                        data = src.read(MAX_SCAN_MEMBER + 1)
                        src.close()
                    except Exception as e:
                        summary.warnings.append(
                            'cannot read nested %s: %s' % (mem.name, e))
                        summary.files += 1
                        continue
                    summary.archives += 1
                    summary.max_depth = max(summary.max_depth, depth + 1)
                    child = None
                    try:
                        child = _open_by_bytes(data, nkind)
                        walk(child, nkind, mem.name, depth + 1)
                    except (zipfile.BadZipFile, tarfile.TarError, OSError,
                            EOFError, ValueError) as e:
                        summary.warnings.append(
                            'unreadable nested archive %s: %s' % (mem.name, e))
                    finally:
                        if child is not None:
                            try:
                                child.close()
                            except Exception:
                                pass
                else:
                    summary.files += 1
                    if mem.size > 0:
                        summary.total_bytes += mem.size
                    if depth >= MAX_DEPTH and mem.looks_like_archive:
                        summary.warnings.append(
                            'max depth %d reached at %s' % (MAX_DEPTH, mem.name))
        finally:
            pass

    try:
        handle = open_archive(path, kind)
    except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError) as e:
        raise ValueError('cannot open archive: %s' % e)
    try:
        walk(handle, kind, os.path.basename(archive_path), 0)
    finally:
        try:
            handle.close()
        except Exception:
            pass

    summary.top_level_entries = max(seen_tops[0], 1)
    return summary


# ------------------------------------------------------------------ extraction

class _Ctx:
    def __init__(self, ws_root, key, cancel, log):
        self.ws_root = ws_root
        self.key = key
        self.cancel = cancel
        self.log = log
        self.base = os.path.join(ws_root, 'archives', key)
        self.seq = 0
        self.total_bytes = 0
        self.entries: List[FileEntry] = []
        self.budget_warned = False

    def alloc(self) -> str:
        p = data_path(self.ws_root, self.key, self.seq)
        self.seq += 1
        ensure_dir(os.path.dirname(p))
        return p

    def rel(self, dest: str) -> str:
        return os.path.relpath(dest, self.base).replace(os.sep, '/')


def _unlink(path: str) -> None:
    try:
        os.unlink(win_long(path))
    except OSError:
        pass


def _copy_member(src, dst_f, cancel: CancelToken):
    """Streaming copy that classifies content in the same pass.

    The head is read and written first, so one pass yields both the magic bytes
    and the file -- the old extractall() could do neither.
    """
    head = src.read(HEAD_LEN)
    kind = sniff(head)
    dst_f.write(head)
    written = len(head)
    while True:
        cancel.raise_if_cancelled()
        buf = src.read(CHUNK)
        if not buf:
            break
        dst_f.write(buf)
        written += len(buf)
    return kind, written, head


def _process_member(mem: _Member, vp: str, depth: int, ctx: _Ctx, cancel,
                    warnings: List[str]) -> None:
    """Extract one member; recurse immediately if it is itself an archive."""
    cancel.raise_if_cancelled()
    if not _check_ratio(mem, warnings.append):
        return
    if len(ctx.entries) >= MAX_ENTRIES or ctx.total_bytes >= MAX_TOTAL_BYTES:
        # Warn exactly once. _BudgetReached is re-raised out through every
        # enclosing _walk, so without this guard a single budget stop emits one
        # 'stopping inside ...' warning per nesting level per remaining member.
        if not ctx.budget_warned:
            ctx.budget_warned = True
            warnings.append(
                'budget reached at %s (%d files, %s): stopping' % (
                    vp, len(ctx.entries), human_bytes(ctx.total_bytes)))
        raise _BudgetReached()

    dest = ctx.alloc()
    try:
        src = mem.open()
        if src is None:
            _unlink(dest)
            return
        try:
            with open(win_long(dest), 'wb') as out:
                nkind, written, head = _copy_member(src, out, cancel)
        finally:
            try:
                src.close()
            except Exception:
                pass
    except Cancelled:
        _unlink(dest)
        raise
    except Exception as e:
        _unlink(dest)
        warnings.append('cannot extract %s: %s' % (vp, e))
        return

    if written > MAX_SINGLE_FILE:
        _unlink(dest)
        warnings.append('skipping %s: %s exceeds the per-file cap' % (
            vp, human_bytes(written)))
        return

    nested = None
    if nkind in ARCHIVE_KINDS:
        if depth < MAX_DEPTH:
            try:
                nested = open_archive(dest, nkind)
            except Exception as e:
                warnings.append('cannot open nested %s: %s' % (vp, e))
        else:
            warnings.append('max depth %d reached at %s' % (MAX_DEPTH, vp))

    if nested is None:
        ctx.entries.append(FileEntry(
            id=_seq_of(dest), vp=vp, disk=ctx.rel(dest), size=written,
            depth=depth, parent=vp.rsplit('/', 1)[0] if '/' in vp else None,
            binary=b'\x00' in head))
        ctx.total_bytes += written
        return

    try:
        _walk(nested, nkind, mem.name, vp, depth + 1, ctx, cancel, warnings)
    except _BudgetReached:
        pass          # already reported once, at the point the budget was hit
    finally:
        try:
            nested.close()
        except Exception:
            pass
    # Intermediates are scaffolding: their contents are already on disk, so
    # keeping them would cost ~200MB of dead weight per archive.
    _unlink(dest)


class _BudgetReached(Exception):
    pass


def _walk(handle, kind: str, source_name: str, vp_prefix: str, depth: int,
          ctx: _Ctx, cancel: CancelToken, warnings: List[str]) -> None:
    cancel.raise_if_cancelled()
    if depth > MAX_DEPTH:
        warnings.append('max depth %d reached at %s' % (MAX_DEPTH, vp_prefix))
        return
    for mem in _members(handle, kind, source_name):
        cancel.raise_if_cancelled()
        _process_member(mem, join_vp(vp_prefix, mem.name), depth, ctx, cancel, warnings)


def _seq_of(dest: str) -> int:
    return int(os.path.basename(dest), 16)


def extract_archive(archive_path: str, ws_root: str, key: str,
                    cancel: Optional[CancelToken] = None,
                    progress: Optional[Callable] = None,
                    log: Optional[Callable] = None,
                    resume: bool = False,
                    force: bool = False,
                    structural: Optional[ScanSummary] = None) -> Manifest:
    """Recursively extract, committing after each completed top-level entry.

    Interrupted runs leave manifest.partial.jsonl recording exactly which
    top-level entries finished, so neither a resumed run nor a later search can
    mistake partial coverage for full coverage.
    """
    cancel = cancel or CancelToken()
    log = log or (lambda m: None)
    progress = progress or (lambda *a: None)
    structural = structural or ScanSummary()

    state, existing, partial_entries, done_tops = inspect_workspace(ws_root, key)
    if state == 'complete' and existing is not None and not force:
        log('using cached extraction for %s (%d files)' % (key, len(existing.entries)))
        return existing

    resumed = bool(resume and state == 'partial' and partial_entries)
    ctx = _Ctx(ws_root, key, cancel, log)
    if resumed:
        entries = list(partial_entries)
        done = set(done_tops)
        base = os.path.join(ws_root, 'archives', key)
        maxid = max((e.id for e in entries), default=-1)
        # Continue the sequence past everything the JSONL already allocated.
        ctx.seq = max(maxid + 1, _max_seq_on_disk(base))
        ctx.total_bytes = sum(e.size for e in entries)
        log('resuming: %d/%d parts already complete' % (
            len(done), structural.top_level_entries))
    else:
        rmtree_robust(os.path.join(ws_root, 'archives', key))
        entries = []
        done = set()
    # ctx.entries must BE the local list, not a copy: _process_member appends
    # to ctx.entries and the caller slices the same object for partial commits.
    ctx.entries = entries

    begin_extraction(ws_root, key)

    try:
        st = os.stat(win_long(archive_path))
        a_size, a_mtime = st.st_size, st.st_mtime_ns
    except OSError:
        a_size, a_mtime = 0, 0

    path = win_long(archive_path)
    kind = sniff_file(path)
    if kind not in ARCHIVE_KINDS:
        raise ValueError('not a recognised archive (magic: %s)' % (kind or 'unknown'))

    warnings: List[str] = []
    total_tops = max(structural.top_level_entries, 1)
    completed = len(done)
    root_name = os.path.basename(archive_path)

    try:
        handle = open_archive(path, kind)
    except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError) as e:
        raise ValueError('cannot open archive: %s' % e)

    try:
        for ti, mem in enumerate(_members(handle, kind, root_name)):
            cancel.raise_if_cancelled()
            if ti in done:
                continue
            start = len(entries)
            vp = join_vp(root_name, mem.name)
            try:
                _process_member(mem, vp, 0, ctx, cancel, warnings)
            except _BudgetReached:
                warnings.append('budget reached at top-level entry %s' % mem.name)
                # Deliberately neither journal nor mark this entry complete.
                # The journal's granularity is one top-level entry, so marking
                # an entry that stopped mid-way as done makes a resume skip its
                # remaining members -- silent file loss. Leaving it unrecorded
                # means resume redoes the whole entry; the partial copies it
                # already wrote stay on disk unlisted, and the sequence
                # allocator steps past them, so no id or vp collides.
                break
            except Cancelled:
                raise
            except Exception as e:
                warnings.append('top-level entry %s failed: %s' % (mem.name, e))
            append_partial(ws_root, key, entries[start:], ti)
            # Record the completion in-memory too: on a fresh run `done` starts
            # empty, and the partial manifest built on cancellation reports its
            # size, so without this a cancelled run would claim 0 of 90 parts
            # complete despite 25 being safely on disk.
            done.add(ti)
            completed += 1
            progress('extract', completed, total_tops,
                     '%d files' % len(ctx.entries))
    except Cancelled:
        man = build_partial_manifest(ws_root, key, archive_path, entries, done,
                                     structural)
        man.warnings.extend(warnings)
        log('cancelled after %d/%d parts, %d files' % (completed, total_tops, len(entries)))
        return man
    else:
        if ctx.budget_warned:
            # The budget stop broke out of the loop, so coverage is incomplete.
            # Falling through to the 'complete' manifest below would both
            # misreport the status AND delete manifest.partial.jsonl (the only
            # record of what did finish), so take the partial exit instead.
            # Without this a budget stop is indistinguishable from a full
            # extraction on the next launch.
            man = build_partial_manifest(ws_root, key, archive_path, entries,
                                         done, structural)
            man.warnings.extend(warnings)
            log('budget reached after %d/%d parts, %d files' % (
                len(done), total_tops, len(entries)))
            return man
    finally:
        try:
            handle.close()
        except Exception:
            pass

    man = Manifest(
        status='complete', archive_name=root_name,
        archive_path=os.path.abspath(archive_path), archive_size=a_size,
        archive_mtime_ns=a_mtime, created_utc=_utc_now(),
        stats={
            'archives': structural.archives,
            'files': len(entries),
            'total_bytes': ctx.total_bytes,
            'max_depth': max((e.depth for e in entries), default=0),
            'top_level_entries': total_tops,
            'completed_entries': completed,
        },
        warnings=warnings, entries=entries)
    write_manifest(ws_root, key, man)
    return man


def _max_seq_on_disk(base: str) -> int:
    """Highest sequence id already materialised under data/, for resume."""
    top = os.path.join(base, 'data')
    if not os.path.isdir(win_long(top)):
        return 0
    best = 0
    for shard in os.listdir(win_long(top)):
        d = os.path.join(top, shard)
        if not os.path.isdir(win_long(d)):
            continue
        for fn in os.listdir(win_long(d)):
            try:
                best = max(best, int(fn, 16) + 1)
            except ValueError:
                pass
    return best
