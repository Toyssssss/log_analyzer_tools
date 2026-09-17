import re
from typing import List, Optional, Sequence, Tuple

from .search import _AFTER_S, _BEFORE_S

MAX_DETAIL_BYTES = 8 << 20      # guard: no log file here exceeds 0.5 MB
MAX_DETAIL_LINES = 60_000

# Separators inside the in-file filter box. Space and comma both work, so
# "522 517 sub_temp" and "522,517,sub_temp" mean the same thing.
_TERM_SPLIT = re.compile(r'[\s,;]+')


def parse_terms(raw: str) -> List[str]:
    """Split a filter box into terms, preserving order and dropping dupes.

    Process/thread numbers are NOT treated specially: measured on the real
    corpus only 19.1% of lines are hilog-shaped, so the rest (kmsg, el2, JSON)
    have no pid column to parse. Matching every term as literal text is the
    only rule that behaves the same across all of them.
    """
    out: List[str] = []
    seen = set()
    for t in _TERM_SPLIT.split(raw or ''):
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def terms_pattern(terms: Sequence[str], match_case: bool) -> Optional[re.Pattern]:
    """One alternation matching ANY term (OR), so a line is scanned once."""
    terms = [t for t in terms if t]
    if not terms:
        return None
    # Longest first: with alternation, re picks the leftmost branch that
    # matches, so 'sub' listed before 'sub_temp' would highlight the shorter
    # span and leave the rest unstyled.
    terms = sorted(set(terms), key=len, reverse=True)
    body = '|'.join(re.escape(t) for t in terms)
    flags = 0 if match_case else re.IGNORECASE
    return re.compile(body, flags)


def line_matches(line: str, terms: Sequence[str], match_case: bool) -> bool:
    """True if the line carries ANY term -- an empty term list matches all.

    Compiled per call rather than cached: the filter box is applied on a
    user's keystroke-then-Enter, not in a per-byte loop, so a cache would be
    complexity without a measurable win.
    """
    pat = terms_pattern(terms, match_case)
    return True if pat is None else pat.search(line) is not None


def keyword_pattern(keyword: str, match_case: bool = False,
                    whole_word: bool = False) -> Optional[re.Pattern]:
    """The original search keyword as a display-side regex.

    Whole-word uses the same explicit lookarounds as the search core (not \b,
    which is wrong next to CJK), so what the pane calls a match is exactly what
    the search called a match.
    """
    if not keyword:
        return None
    body = re.escape(keyword)
    if whole_word:
        body = _BEFORE_S.pattern + body + _AFTER_S.pattern
    flags = 0 if match_case else re.IGNORECASE
    return re.compile(body, flags)


def match_ranges(line: str, terms: Sequence[str], match_case: bool,
                 patterns: Sequence[Optional[re.Pattern]] = ()) -> List[Tuple[int, int]]:
    """(start, end) spans for every occurrence of every term, merged.

    `patterns` carries extra regexes to highlight as well -- the original
    search keyword -- so a line only needs walking once for all of them.

    Overlapping spans are merged so tag_add never receives a nested range,
    which Tk renders inconsistently.
    """
    pats = list(patterns)
    term_pat = terms_pattern(terms, match_case)
    if term_pat is not None:
        pats.append(term_pat)

    spans: List[Tuple[int, int]] = []
    for p in pats:
        if p is None:
            continue
        for m in p.finditer(line):
            if m.end() > m.start():
                spans.append((m.start(), m.end()))
    if not spans:
        return []
    spans.sort()
    merged = [spans[0]]
    for s, e in spans[1:]:
        ls, le = merged[-1]
        if s <= le:
            if e > le:
                merged[-1] = (ls, e)
        else:
            merged.append((s, e))
    return merged


def read_lines(source, entry, codec: str, max_bytes: int = MAX_DETAIL_BYTES,
               max_lines: int = MAX_DETAIL_LINES):
    """Read a leaf file as display lines. Returns (lines, truncated).

    Bounded so a pathologically large file cannot wedge the UI; the real
    corpus tops out at 524 KB / ~6,200 lines, which renders in milliseconds.
    """
    from .textio import decode_line

    lines: List[str] = []
    truncated = False
    total = 0
    with source.open(entry) as fh:
        for raw in fh:
            total += len(raw)
            if total > max_bytes or len(lines) >= max_lines:
                truncated = True
                break
            lines.append(decode_line(raw, codec))
    return lines, truncated


def select_lines(lines: Sequence[str], terms: Sequence[str], match_case: bool,
                 keep: Optional[Sequence[int]] = None) -> List[Tuple[int, str]]:
    """Pick which lines to show as (index, line), 0-based index.

    Two modes, and the filter plays a different role in each:

    - `keep` is None ('all file text'): every line is shown. The filter narrows
      nothing here -- it only decides what gets highlighted -- so switching to
      this mode always reveals the whole file.
    - `keep` is a hit list ('matched lines only'): the shown set is the UNION of
      the keyword hits and the filter matches. A line is shown if it is a hit
      OR it matches a filter term, so searching inside the file widens what you
      see rather than emptying the view whenever the filter term happens not to
      appear among the hits.

    An empty term list makes the filter a no-op, leaving `keep` alone to mean
    'matched lines only'.
    """
    if keep is None:
        # 'All file text' always shows the file, unfiltered.
        return list(enumerate(lines))

    hit_set = {i for i in keep if 0 <= i < len(lines)}
    if not terms:
        return [(i, lines[i]) for i in sorted(hit_set)]

    out: List[Tuple[int, str]] = []
    for i, ln in enumerate(lines):
        if i in hit_set or line_matches(ln, terms, match_case):
            out.append((i, ln))
    return out
