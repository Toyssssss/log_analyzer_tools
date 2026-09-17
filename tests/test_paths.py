import os
import tempfile
import unittest

from log_analyzer.paths import (
    archive_key, atomic_write_json, data_path, free_space, human_bytes,
    read_json, rmtree_robust, win_long, workspace_root,
)


class DataPathTest(unittest.TestCase):
    def test_no_collisions_across_the_entry_budget(self):
        # The shard is derived from seq, so a wrong mask would silently make two
        # different files share a disk path and lose data.
        seen = {}
        for seq in range(300000):
            p = data_path('ws', 'k', seq)
            if p in seen:
                self.fail('collision: seq %d vs %d at %s' % (seq, seen[p], p))
            seen[p] = seq

    def test_shard_boundaries(self):
        self.assertIn(os.path.join('data', '00'), data_path('ws', 'k', 0))
        self.assertIn(os.path.join('data', '00'), data_path('ws', 'k', 4095))
        self.assertIn(os.path.join('data', '01'), data_path('ws', 'k', 4096))
        self.assertIn(os.path.join('data', 'ff'), data_path('ws', 'k', 0xff000))

    def test_path_is_short_even_for_high_seq(self):
        # The whole point: on-disk names never derive from member names, so
        # they must stay well under the NTFS 255-char segment limit.
        p = data_path('C:/' + 'w' * 60, 'f' * 16, 299999)
        self.assertLess(len(p), 200)


class ArchiveKeyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='la-paths-')
        self.p = os.path.join(self.tmp, 'a.zip')
        with open(self.p, 'wb') as f:
            f.write(b'x')

    def tearDown(self):
        rmtree_robust(self.tmp)

    def test_stable_for_unchanged_file(self):
        self.assertEqual(archive_key(self.p), archive_key(self.p))

    def test_changes_when_content_changes(self):
        k1 = archive_key(self.p)
        with open(self.p, 'wb') as f:
            f.write(b'yy')
        self.assertNotEqual(k1, archive_key(self.p))

    def test_length_is_16(self):
        self.assertEqual(len(archive_key(self.p)), 16)

    def test_missing_file_does_not_raise(self):
        self.assertEqual(len(archive_key(os.path.join(self.tmp, 'nope.zip'))), 16)


class RmtreeTest(unittest.TestCase):
    def test_removes_readonly_files(self):
        d = tempfile.mkdtemp(prefix='la-rm-')
        sub = os.path.join(d, 'x.txt')
        with open(sub, 'w') as f:
            f.write('data')
        os.chmod(sub, 0o444)
        rmtree_robust(d)
        self.assertFalse(os.path.exists(d))

    def test_missing_path_is_a_noop(self):
        rmtree_robust(os.path.join(tempfile.gettempdir(), 'definitely-not-here-xyz'))


class WinLongTest(unittest.TestCase):
    def test_short_path_untouched(self):
        p = win_long('a/b')
        self.assertNotIn('\\\\?\\', p)


class WorkspaceRootTest(unittest.TestCase):
    def test_explicit_wins(self):
        self.assertEqual(workspace_root('/tmp/explicit'), os.path.abspath('/tmp/explicit'))

    def test_env_var_used(self):
        old = os.environ.get('LOG_ANALYZER_WORKSPACE')
        os.environ['LOG_ANALYZER_WORKSPACE'] = '/tmp/from-env'
        try:
            self.assertEqual(workspace_root(), os.path.abspath('/tmp/from-env'))
        finally:
            if old is None:
                os.environ.pop('LOG_ANALYZER_WORKSPACE', None)
            else:
                os.environ['LOG_ANALYZER_WORKSPACE'] = old

    def test_default_is_absolute(self):
        os.environ.pop('LOG_ANALYZER_WORKSPACE', None)
        self.assertTrue(os.path.isabs(workspace_root()))


class MiscTest(unittest.TestCase):
    def test_human_bytes(self):
        self.assertEqual(human_bytes(0), '0 B')
        self.assertEqual(human_bytes(1024), '1.0 KB')

    def test_free_space_is_positive(self):
        self.assertGreater(free_space('.'), 0)

    def test_free_space_on_missing_path_walks_up(self):
        self.assertGreater(free_space(os.path.join(tempfile.gettempdir(), 'nope', 'x')), 0)


if __name__ == '__main__':
    unittest.main()
