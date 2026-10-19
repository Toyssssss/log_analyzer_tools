import unittest
import zipfile

from log_analyzer.naming import (
    decode_entry_name, export_stem, join_vp, repair_surrogates, shorten_vp,
)
from tests.fixtures import build_zip_raw_gbk_name


class EntryInfo:
    """Minimal stand-in for zipfile.ZipInfo."""

    def __init__(self, filename, flag_bits=0):
        self.filename = filename
        self.flag_bits = flag_bits


class DecodeEntryNameTest(unittest.TestCase):
    def test_utf8_flag_is_trusted(self):
        info = EntryInfo('温度.log', flag_bits=0x800)
        self.assertEqual(decode_entry_name(info), '温度.log')

    def test_gbk_name_without_flag_is_recovered(self):
        # Simulate what zipfile hands us: cp437 view of raw GBK bytes.
        raw = '温度.log'.encode('gbk')
        cp437_view = raw.decode('cp437')
        info = EntryInfo(cp437_view, flag_bits=0)
        self.assertEqual(decode_entry_name(info), '温度.log')

    def test_ascii_name_is_unchanged(self):
        info = EntryInfo('hilog/kmsg-1.log', flag_bits=0)
        self.assertEqual(decode_entry_name(info), 'hilog/kmsg-1.log')

    def test_real_zip_with_gbk_name_round_trips(self):
        blob = build_zip_raw_gbk_name('温度.log'.encode('gbk'), b'x')
        import io
        z = zipfile.ZipFile(io.BytesIO(blob))
        info = z.infolist()[0]
        self.assertEqual(decode_entry_name(info), '温度.log')

    def test_never_raises_on_undecodable_bytes(self):
        info = EntryInfo(b'\xff\xfe\xfd'.decode('cp437'), flag_bits=0)
        self.assertIsInstance(decode_entry_name(info), str)


class RepairSurrogatesTest(unittest.TestCase):
    def test_clean_string_untouched(self):
        self.assertEqual(repair_surrogates('abc'), 'abc')

    def test_lone_surrogate_replaced(self):
        s = b'\xff'.decode('utf-8', 'surrogateescape')
        out = repair_surrogates(s)
        self.assertNotIn('\udcff', out)
        out.encode('utf-8')   # must not raise


class JoinVpTest(unittest.TestCase):
    def test_appends_with_slash(self):
        self.assertEqual(join_vp('a.zip', 'b/c.log'), 'a.zip/b/c.log')

    def test_empty_prefix(self):
        self.assertEqual(join_vp('', 'a.log'), 'a.log')

    def test_backslashes_normalised(self):
        self.assertEqual(join_vp('a.zip', 'b\\c.log'), 'a.zip/b/c.log')

    def test_absolute_member_name_is_not_rooted(self):
        self.assertEqual(join_vp('a.zip', '/etc/passwd'), 'a.zip/etc/passwd')

    def test_traversal_is_only_cosmetic(self):
        # The name never reaches a filesystem path, so this is display only.
        self.assertEqual(join_vp('a.zip', '../../evil'), 'a.zip/../../evil')


class ExportStemTest(unittest.TestCase):
    def test_keeps_last_two_segments(self):
        # Two segments, not one: the leaf name alone ('el2.log') appears in
        # several directories in this corpus, and the dialog would then offer
        # the same default for all of them.
        vp = 'a.zip/CLUSTER/c/l2.zip/Eventid_x_rda.zip/hilog/el2.log'
        self.assertEqual(export_stem(vp), 'hilog_el2')

    def test_drops_the_members_own_extension(self):
        # The export is always '.txt' (app.py appends it), so keeping '.log'
        # here would only produce 'el2.log.txt'.
        self.assertEqual(export_stem('a.zip/hilog/el2.log'), 'hilog_el2')
        self.assertEqual(export_stem('a.zip/hilog/kmsg-1.log'), 'hilog_kmsg-1')

    def test_single_segment_has_no_extension_either(self):
        self.assertEqual(export_stem('el2.log'), 'el2')

    def test_name_that_is_only_an_extension_survives(self):
        # '.log' has no stem to keep; stripping it would leave 'hilog_' + '.txt'.
        self.assertEqual(export_stem('a.zip/hilog/.log'), 'hilog_.log')

    def test_multi_dot_leaf_keeps_all_but_the_last_part(self):
        self.assertEqual(export_stem('a.zip/hilog/a.b.log'), 'hilog_a.b')

    def test_windows_unsafe_characters_are_replaced(self):
        vp = 'C:\\x.zip\\hilog:1|2?3*4.log'
        out = export_stem(vp)
        for ch in '<>:"/\\|?*':
            self.assertNotIn(ch, out)

    def test_control_bytes_are_replaced(self):
        # A mis-decoded member name can carry control characters; Tk's save
        # dialog would pass them straight through to the filesystem.
        out = export_stem('a.zip/hi\x00lo\x1f.log')
        self.assertNotIn('\x00', out)
        self.assertNotIn('\x1f', out)

    def test_traversal_segments_are_dropped(self):
        # '..' and '.' are filtered out of the segment list entirely, so they
        # can never become the first half of the joined stem.
        self.assertEqual(export_stem('a.zip/./../hilog/el2.log'), 'hilog_el2')

    def test_trailing_slash_does_not_become_the_name(self):
        self.assertEqual(export_stem('a.zip/x.log/'), 'a.zip_x')

    def test_long_name_is_capped_without_losing_the_tail(self):
        vp = 'a.zip/hilog/' + 'x' * 120 + '.log'
        out = export_stem(vp)
        self.assertEqual(len(out), 80)
        self.assertIn('~', out)          # trimmed in the middle, not the end
        self.assertTrue(out.startswith('hilog_'))

    def test_degenerate_paths_fall_back_to_export(self):
        # Only paths with no usable segment left reach the fallback; real
        # virtual paths always end in a member name, so this is a guard
        # against an empty 'initialfile', not a shape the app produces.
        for vp in ('', '///', '/', '.', '..', './../'):
            self.assertEqual(export_stem(vp), 'export')

    def test_result_is_never_empty(self):
        # _UNSAFE_FILENAME.sub can only leave '_' runs, and those collapse, so
        # the only way to an empty stem is a path with no usable segment left.
        for vp in ('a.zip/...', 'a.zip/ ', 'a.zip/*', 'a.zip/  .  '):
            self.assertTrue(export_stem(vp))

    def test_underscore_runs_are_collapsed(self):
        self.assertNotIn('__', export_stem('a.zip/hi::lo.log'))


class ShortenVpTest(unittest.TestCase):
    def test_short_path_unchanged(self):
        self.assertEqual(shorten_vp('a.log'), 'a.log')

    def test_keeps_head_and_tail(self):
        vp = 'root.zip/' + '/'.join('m%d' % i for i in range(30)) + '/leaf.log'
        out = shorten_vp(vp, maxlen=60)
        self.assertLessEqual(len(out), 60)
        self.assertTrue(out.startswith('root.zip/'))
        self.assertTrue(out.endswith('/leaf.log'))
        self.assertIn('…', out)

    def test_length_bound_respected(self):
        vp = 'r.zip/' + '/'.join('x' * 40 for _ in range(20))
        self.assertLessEqual(len(shorten_vp(vp, maxlen=80)), 80)


if __name__ == '__main__':
    unittest.main()