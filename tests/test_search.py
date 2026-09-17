import io
import os
import tempfile
import unittest

from log_analyzer.extractor import CancelToken
from log_analyzer.search import (
    SearchOptions, build_pattern, keyword_is_ascii, search,
)


class FakeSource:
    """In-memory stand-in for DiskSource, so search semantics are testable
    without touching the filesystem or a workspace."""

    def __init__(self, blobs):
        self.blobs = blobs          # {vp: bytes}

    def open(self, entry):
        return io.BytesIO(self.blobs[entry.vp])


class Entry:
    def __init__(self, vp):
        self.vp = vp


def run(lines, keyword, **kw):
    blobs = {'a.log': b'\n'.join(lines)}
    entries = [Entry('a.log')]
    got = []
    s = search(entries, FakeSource(blobs),
               SearchOptions(keyword, **kw),
               cancel=CancelToken(), sink=got.extend)
    return got, s


class PatternTest(unittest.TestCase):
    def test_ascii_keyword_uses_bytes_path(self):
        self.assertTrue(keyword_is_ascii('sub_temp'))
        pat, is_bytes = build_pattern(SearchOptions('sub_temp'))
        self.assertTrue(is_bytes)
        self.assertIsInstance(pat.pattern, bytes)

    def test_non_ascii_keyword_uses_str_path(self):
        self.assertFalse(keyword_is_ascii('温度'))
        pat, is_bytes = build_pattern(SearchOptions('温度'))
        self.assertFalse(is_bytes)
        self.assertIsInstance(pat.pattern, str)

    def test_keyword_is_escaped_not_regex(self):
        pat, _ = build_pattern(SearchOptions('a.c'))
        self.assertNotRegex(b'axc', pat)
        self.assertRegex(b'a.c', pat)


class WholeWordTest(unittest.TestCase):
    """The boundary must be a real NOT-word-after, not a lookahead that
    requires a word character -- the latter inverts the meaning entirely."""

    def test_exact_word_matches(self):
        got, _ = run([b'I sub_temp: 5'], 'sub_temp', whole_word=True)
        self.assertEqual(len(got), 1)

    def test_prefix_does_not_match(self):
        got, _ = run([b'I sub_temp_extra: 5'], 'sub_temp', whole_word=True)
        self.assertEqual(got, [])

    def test_suffix_does_not_match(self):
        got, _ = run([b'I xsub_temp: 5'], 'sub_temp', whole_word=True)
        self.assertEqual(got, [])

    def test_without_whole_word_prefix_matches(self):
        got, _ = run([b'I sub_temp_extra: 5'], 'sub_temp', whole_word=False)
        self.assertEqual(len(got), 1)

    def test_cjk_adjacent_is_a_boundary(self):
        # \\b would NOT treat this as a boundary because 度 is a non-word char
        # to ASCII \\b; the explicit lookaround deliberately does.
        line = 'msg: 温度sub_temp'.encode('utf-8')
        got, _ = run([line], 'sub_temp', whole_word=True)
        self.assertEqual(len(got), 1)

    def test_non_ascii_whole_word(self):
        line = '温度=5'.encode('utf-8')
        got, _ = run([line], '温度', whole_word=True)
        self.assertEqual(len(got), 1)
        got, _ = run(['温度传感器=5'.encode('utf-8')],
                     '温度', whole_word=True)
        self.assertEqual(got, [])


class CaseTest(unittest.TestCase):
    def test_default_is_case_insensitive(self):
        got, _ = run([b'ERROR happened'], 'error')
        self.assertEqual(len(got), 1)

    def test_match_case_respected(self):
        got, _ = run([b'ERROR happened'], 'error', match_case=True)
        self.assertEqual(got, [])

    def test_whole_word_and_case_compose(self):
        got, _ = run([b'ERROR happened'], 'error', whole_word=True)
        self.assertEqual(len(got), 1)
        got, _ = run([b'ERRORS happened'], 'error', whole_word=True)
        self.assertEqual(got, [])


class CountingTest(unittest.TestCase):
    def test_all_hits_reported_with_line_numbers(self):
        got, s = run([b'x 1', b'sub_temp a', b'y 2', b'sub_temp b'], 'sub_temp')
        self.assertEqual(s.matched_files, 1)
        self.assertEqual(s.total_hits, 2)
        self.assertEqual([h.line_no for h in got[0].hits], [2, 4])

    def test_only_first_match_per_file_used_to_be_reported(self):
        # Regression guard for the old searcher's `break` after one hit.
        lines = [b'sub_temp %d' % i for i in range(10)]
        got, s = run(lines, 'sub_temp')
        self.assertEqual(s.total_hits, 10)
        self.assertEqual(len(got[0].hits), 10)

    def test_per_file_cap_is_reported_as_truncated(self):
        lines = [b'sub_temp %d' % i for i in range(50)]
        got, s = run(lines, 'sub_temp', max_hits_per_file=10)
        self.assertEqual(len(got[0].hits), 10)
        self.assertEqual(got[0].total_hits, 50)
        self.assertTrue(got[0].truncated)

    def test_leading_whitespace_preserved(self):
        got, _ = run([b'  I sub_temp: 5'], 'sub_temp')
        self.assertTrue(got[0].hits[0].line.startswith('  I '))

    def test_match_cannot_span_lines(self):
        got, _ = run([b'sub_', b'temp'], 'sub_temp')
        self.assertEqual(got, [])


class EncodingTest(unittest.TestCase):
    def test_gbk_content_decoded_for_display(self):
        line = '温度 ' .encode('gbk') + b'sub_temp'
        got, _ = run([line], 'sub_temp')
        self.assertIn('温度', got[0].hits[0].line)

    def test_invalid_bytes_do_not_raise(self):
        got, _ = run([b'\xff\xfe sub_temp'], 'sub_temp')
        self.assertEqual(len(got), 1)

    def test_ascii_match_inside_gbk_file(self):
        blob = '中文'.encode('gbk') + b'\nsub_temp here\n'
        got, _ = run(blob.split(b'\n'), 'sub_temp')
        self.assertEqual(len(got), 1)


class BinaryTest(unittest.TestCase):
    def test_binary_included_by_default(self):
        got, _ = run([b'\x00\x01 sub_temp \x00'], 'sub_temp')
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].binary)

    def test_binary_can_be_excluded(self):
        got, _ = run([b'\x00\x01 sub_temp \x00'], 'sub_temp', include_binary=False)
        self.assertEqual(got, [])


class NameFilterTest(unittest.TestCase):
    def test_name_filter_skips_non_matching(self):
        blobs = {'a.log': b'sub_temp', 'b.txt': b'sub_temp'}
        entries = [Entry('a.log'), Entry('b.txt')]
        got = []
        search(entries, FakeSource(blobs),
               SearchOptions('sub_temp', name_filter='.txt'),
               cancel=CancelToken(), sink=got.extend)
        self.assertEqual([f.entry.vp for f in got], ['b.txt'])


class CancelTest(unittest.TestCase):
    """Cancelling must stop the scan and never be recorded as a file error.

    Cancelled subclasses Exception, so a per-file `except Exception` handler
    silently converts a cancellation into a bogus 'unreadable file' entry.
    """

    def test_cancel_is_not_reported_as_an_error(self):
        blobs = {'f%d.log' % i: b'sub_temp\n' for i in range(50)}
        entries = [Entry('f%d.log' % i) for i in range(50)]
        cancel = CancelToken()
        got = []

        def sink(batch):
            got.extend(batch)
            cancel.cancel()          # cancel mid-run, from the worker thread

        s = search(entries, FakeSource(blobs), SearchOptions('sub_temp'),
                   cancel=cancel, sink=sink)
        self.assertTrue(s.cancelled)
        self.assertEqual(s.errors, 0)
        self.assertFalse([f for f in got if f.error])
        self.assertLess(s.scanned, 50)


if __name__ == '__main__':
    unittest.main()
