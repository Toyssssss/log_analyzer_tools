import unittest
import zipfile

from log_analyzer.naming import (
    decode_entry_name, join_vp, repair_surrogates, shorten_vp,
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