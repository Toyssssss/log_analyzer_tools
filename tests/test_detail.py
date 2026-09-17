import os
import tempfile
import unittest

from log_analyzer.detail import (
    line_matches, match_ranges, parse_terms, read_lines, select_lines,
)


class ParseTermsTest(unittest.TestCase):
    def test_space_and_comma_both_split(self):
        self.assertEqual(parse_terms('522 517 sub_temp'), ['522', '517', 'sub_temp'])
        self.assertEqual(parse_terms('522,517,sub_temp'), ['522', '517', 'sub_temp'])
        self.assertEqual(parse_terms('522, 517;sub_temp'), ['522', '517', 'sub_temp'])

    def test_blank_yields_nothing(self):
        self.assertEqual(parse_terms(''), [])
        self.assertEqual(parse_terms('   '), [])
        self.assertEqual(parse_terms(None), [])

    def test_duplicates_collapse_but_order_is_kept(self):
        self.assertEqual(parse_terms('b a b'), ['b', 'a'])


class LineMatchTest(unittest.TestCase):
    LINE = '08-28 00:23:09.180  506  522 I HUTM_McuAdapter: msg key is : sub_temp'

    def test_terms_are_ored(self):
        self.assertTrue(line_matches(self.LINE, ['522'], False))
        self.assertTrue(line_matches(self.LINE, ['517', 'sub_temp'], False))
        self.assertFalse(line_matches(self.LINE, ['999', 'nope'], False))

    def test_process_number_matches_as_plain_text(self):
        # The whole point: 522 is matched wherever it sits, because 81% of the
        # corpus has no hilog pid column to parse.
        self.assertTrue(line_matches(self.LINE, ['522'], False))

    def test_case_folding(self):
        self.assertTrue(line_matches(self.LINE, ['SUB_TEMP'], False))
        self.assertFalse(line_matches(self.LINE, ['SUB_TEMP'], True))

    def test_empty_terms_match_everything(self):
        self.assertTrue(line_matches(self.LINE, [], False))


class MatchRangeTest(unittest.TestCase):
    def test_offsets_point_at_the_term(self):
        line = 'abc sub_temp xyz'
        self.assertEqual(match_ranges(line, ['sub_temp'], False), [(4, 12)])

    def test_multiple_occurrences(self):
        self.assertEqual(match_ranges('a a a', ['a'], False), [(0, 1), (2, 3), (4, 5)])

    def test_overlapping_terms_are_merged(self):
        # Tk renders nested tag ranges inconsistently, so they must be flat.
        self.assertEqual(match_ranges('sub_temp', ['sub', 'sub_temp'], False),
                         [(0, 8)])

    def test_adjacent_terms_merge(self):
        self.assertEqual(match_ranges('ab', ['a', 'b'], False), [(0, 2)])

    def test_no_match_is_empty(self):
        self.assertEqual(match_ranges('abc', ['zz'], False), [])

    def test_match_case_respected(self):
        self.assertEqual(match_ranges('ABC', ['abc'], True), [])
        self.assertEqual(match_ranges('ABC', ['abc'], False), [(0, 3)])


class SelectLinesTest(unittest.TestCase):
    LINES = ['aaa', 'bbb sub_temp', 'ccc', 'ddd sub_temp']

    def test_no_terms_returns_everything(self):
        got = select_lines(self.LINES, [], False)
        self.assertEqual([i for i, _ in got], [0, 1, 2, 3])

    def test_keep_restricts_the_base_set(self):
        got = select_lines(self.LINES, [], False, keep=[1, 3])
        self.assertEqual([i for i, _ in got], [1, 3])

    def test_keep_and_filter_are_unioned(self):
        # 'Matched lines only': a line is shown if it is a hit OR it matches
        # the filter. Here hits are 1 and 3, and 'ccc' matches only line 2, so
        # all three appear -- the filter adds to the view rather than
        # replacing it.
        got = select_lines(self.LINES, ['ccc'], False, keep=[1, 3])
        self.assertEqual([i for i, _ in got], [1, 2, 3])

    def test_filter_matching_a_hit_does_not_duplicate_it(self):
        got = select_lines(self.LINES, ['bbb'], False, keep=[1, 3])
        self.assertEqual([i for i, _ in got], [1, 3])

    def test_all_text_mode_ignores_the_filter(self):
        # 'All file text' (keep=None) always shows every line; the filter only
        # decides what gets highlighted. Narrowing here would make switching
        # to this mode appear to hide the rest of the file.
        got = select_lines(self.LINES, ['ccc'], False)
        self.assertEqual([i for i, _ in got], [0, 1, 2, 3])

    def test_out_of_range_kept_indices_are_dropped(self):
        got = select_lines(self.LINES, [], False, keep=[1, 99])
        self.assertEqual([i for i, _ in got], [1])


class FakeSource:
    def __init__(self, blob):
        self.blob = blob

    def open(self, entry):
        import io
        return io.BytesIO(self.blob)


class ReadLinesTest(unittest.TestCase):
    def test_splits_and_decodes(self):
        src = FakeSource(b'one\ntwo\n')
        lines, trunc = read_lines(src, object(), 'utf-8')
        self.assertEqual(lines, ['one', 'two'])
        self.assertFalse(trunc)

    def test_missing_trailing_newline_still_yields_last_line(self):
        src = FakeSource(b'one\ntwo')
        lines, _ = read_lines(src, object(), 'utf-8')
        self.assertEqual(lines, ['one', 'two'])

    def test_byte_cap_truncates(self):
        src = FakeSource(b'x' * 100 + b'\n')
        lines, trunc = read_lines(src, object(), 'utf-8', max_bytes=10)
        self.assertTrue(trunc)
        self.assertLess(len(lines), 3)

    def test_line_cap_truncates(self):
        src = FakeSource(b'a\nb\nc\nd\n')
        lines, trunc = read_lines(src, object(), 'utf-8', max_lines=2)
        self.assertTrue(trunc)
        self.assertEqual(len(lines), 2)

    def test_empty_file(self):
        src = FakeSource(b'')
        lines, trunc = read_lines(src, object(), 'utf-8')
        self.assertEqual(lines, [])
        self.assertFalse(trunc)


if __name__ == '__main__':
    unittest.main()
