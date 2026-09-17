import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator, List, Optional

from .extractor import CancelToken, Cancelled
from .textio import decode_line, detect_codec, is_binary_chunk

READ_CHUNK = 1 << 20
SAMPLE_SIZE = 64 * 1024
CANCEL_EVERY_LINES = 4096

MAX_HITS_PER_FILE = 500
MAX_TOTAL_HITS = 200_000
BATCH_FILES = 25
BATCH_SECONDS = 0.15

# Word characters for the whole-word boundary. Deliberately explicit rather
# than \b, which is ASCII-word-based: for a CJK-adjacent keyword \b fires a
# boundary where a human sees none ('msg: 温度sub_temp' would match sub_temp
# under \b-whole-word because 度 is a non-word char to \b).
WORD_BYTES = b'A-Za-z0-9_'
_BEFORE_B = re.compile(b'(?<![' + WORD_BYTES + b'])')
_AFTER_B = re.compile(b'(?![' + WORD_BYTES + b'])')
_BEFORE_S = re.compile(r'(?<!\w)')
_AFTER_S = re.compile(r'(?!\w)')


@dataclass(frozen=True)
class SearchOptions:
    keyword: str
    match_case: bool = False      # 'Match case' unchecked -> case-insensitive
    whole_word: bool = False
    include_binary: bool = True
    name_filter: Optional[str] = None    # substring match on the virtual path
    max_hits_per_file: int = MAX_HITS_PER_FILE
    max_total_hits: int = MAX_TOTAL_HITS


@dataclass
class Hit:
    line_no: int
    line: str


@dataclass
class FileHits:
    entry: object
    hits: List[Hit]
    total_hits: int          # true count, may exceed len(hits)
    truncated: bool
    codec: str
    binary: bool
    error: Optional[str] = None


@dataclass
class SearchSummary:
    scanned: int = 0
    matched_files: int = 0
    total_hits: int = 0
    elapsed: float = 0.0
    truncated: bool = False
    cancelled: bool = False
    errors: int = 0

    def describe(self) -> str:
        bits = ['scanned %d files' % self.scanned,
                '%d hits in %d files' % (self.total_hits, self.matched_files),
                '%.1fs' % self.elapsed]
        if self.truncated:
            bits.append('truncated')
        if self.cancelled:
            bits.append('cancelled')
        if self.errors:
            bits.append('%d unreadable' % self.errors)
        return ' · '.join(bits)


def keyword_is_ascii(keyword: str) -> bool:
    try:
        keyword.encode('ascii')
        return True
    except UnicodeEncodeError:
        return False


def build_pattern(opts: SearchOptions):
    """Return (compiled_pattern, is_bytes_pattern).

    Keyword is always re.escape'd: this is literal substring search, not a
    regex mode. ASCII keywords take the bytes path, which avoids decoding every
    file and is measurably faster on this corpus.
    """
    if keyword_is_ascii(opts.keyword):
        pat = re.escape(opts.keyword.encode('ascii'))
        if opts.whole_word:
            pat = _BEFORE_B.pattern + pat + _AFTER_B.pattern
        flags = 0 if opts.match_case else re.IGNORECASE
        return re.compile(pat, flags), True
    pat = re.escape(opts.keyword)
    if opts.whole_word:
        # Explicit lookarounds keep the rule identical on both paths and
        # independent of re.IGNORECASE for the boundary classes.
        pat = _BEFORE_S.pattern + pat + _AFTER_S.pattern
    flags = re.UNICODE if opts.match_case else (re.UNICODE | re.IGNORECASE)
    return re.compile(pat, flags), False


def _iter_lines(fh, cancel: CancelToken) -> Iterator[bytes]:
    """Chunked line iteration with a carry buffer for split lines.

    Matching is per-line, so a match can never span a line boundary.
    """
    carry = b''
    while True:
        cancel.raise_if_cancelled()
        buf = fh.read(READ_CHUNK)
        if not buf:
            break
        buf = carry + buf
        parts = buf.split(b'\n')
        carry = parts.pop()
        for p in parts:
            yield p
    if carry:
        yield carry


def _scan_file(fh, pat, is_bytes: bool, codec: str, opts: SearchOptions,
               cancel: CancelToken):
    """Scan one file. Returns (hits, total_hits, truncated).

    is_bytes selects the match path; codec is independent and governs display
    only. An ASCII keyword on a GBK file still takes the bytes path but must
    still be decoded as GBK for display.
    """
    hits: List[Hit] = []
    total = 0
    truncated = False
    since_check = 0
    for line_no, raw in enumerate(_iter_lines(fh, cancel), start=1):
        since_check += 1
        if since_check >= CANCEL_EVERY_LINES:
            since_check = 0
            cancel.raise_if_cancelled()
        if is_bytes:
            matched = pat.search(raw) is not None
        else:
            matched = pat.search(raw.decode(codec, errors='replace')) is not None
        if not matched:
            continue
        total += 1
        if len(hits) < opts.max_hits_per_file:
            # Decode only what we display; on the bytes path the match decision
            # was made on the exact bytes on disk, so encoding can never affect
            # the result.
            hits.append(Hit(line_no, decode_line(raw, codec)))
        else:
            truncated = True
    return hits, total, truncated


def search(entries, source, opts: SearchOptions, *, cancel: CancelToken,
           sink: Callable[[List[FileHits]], None],
           progress: Optional[Callable] = None) -> SearchSummary:
    """Stream every entry, emitting matching files through sink in batches.

    sink and progress run on the worker thread and must not touch tkinter.
    """
    pat, is_bytes = build_pattern(opts)
    summary = SearchSummary()
    t0 = time.time()
    total_entries = max(len(entries), 1)

    batch: List[FileHits] = []
    last_flush = time.time()

    def flush():
        nonlocal last_flush
        if batch:
            sink(list(batch))
            batch.clear()
        last_flush = time.time()

    try:
        for entry in entries:
            cancel.raise_if_cancelled()
            summary.scanned += 1
            if opts.name_filter and opts.name_filter not in entry.vp:
                if progress and summary.scanned % 64 == 0:
                    progress('search', summary.scanned, total_entries, '')
                continue
            fh = None
            try:
                fh = source.open(entry)
                sample = fh.read(SAMPLE_SIZE)
                fh.seek(0)
                binary = is_binary_chunk(sample)
                if binary and not opts.include_binary:
                    continue
                # Codec is always detected, even on the bytes path: it governs
                # display only, and an ASCII keyword in a GBK file is common.
                codec = detect_codec(sample)
                hits, total, truncated = _scan_file(
                    fh, pat, is_bytes, codec, opts, cancel)
            except Cancelled:
                # Must be re-raised, not recorded: Cancelled is an Exception,
                # so letting it fall into the handler below would log one bogus
                # 'unreadable file' per cancelled run and attach a junk error
                # node to the results.
                raise
            except Exception as e:
                summary.errors += 1
                batch.append(FileHits(entry, [], 0, False, 'utf-8', False,
                                      error=str(e)))
                continue
            finally:
                if fh is not None:
                    try:
                        fh.close()
                    except Exception:
                        pass

            if total:
                summary.matched_files += 1
                summary.total_hits += total
                batch.append(FileHits(entry, hits, total, truncated, codec, binary))

            if len(batch) >= BATCH_FILES or (time.time() - last_flush) >= BATCH_SECONDS:
                flush()
            if summary.total_hits >= opts.max_total_hits:
                summary.truncated = True
                break
            if progress and summary.scanned % 64 == 0:
                progress('search', summary.scanned, total_entries,
                         '%d hits' % summary.total_hits)
    except Cancelled:
        summary.cancelled = True
    finally:
        flush()

    summary.elapsed = time.time() - t0
    return summary
