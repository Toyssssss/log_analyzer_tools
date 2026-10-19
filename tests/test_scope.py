import os
import shutil
import tempfile
import unittest

from log_analyzer.flat import scan_flat
from log_analyzer.manifest import Manifest
from log_analyzer.scope import for_archive, for_flat
from log_analyzer.source import DiskSource, FlatSource


class ArchiveScopeTest(unittest.TestCase):
    def test_carries_the_key_and_a_disk_source(self):
        man = Manifest(archive_name='a.zip', entries=[])
        scope = for_archive('/tmp/ws', 'abc123', man)
        self.assertIsInstance(scope.source, DiskSource)
        self.assertEqual(scope.key, 'abc123')
        self.assertEqual(scope.root, '/tmp/ws')
        self.assertFalse(scope.flat)

    def test_entries_come_from_the_manifest(self):
        man = Manifest(archive_name='a.zip', entries=['sentinel'])
        self.assertEqual(for_archive('/tmp/ws', 'k', man).entries, ['sentinel'])


class FlatScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='la-scope-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_has_no_key_and_a_flat_source(self):
        man = scan_flat(self.tmp)
        scope = for_flat(self.tmp, man)
        self.assertIsInstance(scope.source, FlatSource)
        self.assertIsNone(scope.key)
        self.assertTrue(scope.flat)

    def test_folder_base_is_the_folder(self):
        scope = for_flat(self.tmp, scan_flat(self.tmp))
        self.assertEqual(scope.source.base, os.path.abspath(self.tmp))

    def test_single_file_base_is_the_parent_dir(self):
        # Without this, FlatSource would join a path onto a file name.
        path = os.path.join(self.tmp, 'only.log')
        with open(path, 'wb') as f:
            f.write(b'x')
        scope = for_flat(path, scan_flat(path))
        self.assertEqual(scope.source.base, os.path.abspath(self.tmp))
        self.assertEqual(scope.root, os.path.abspath(path))
        with scope.source.open(scope.entries[0]) as fh:
            self.assertEqual(fh.read(), b'x')


if __name__ == '__main__':
    unittest.main()
