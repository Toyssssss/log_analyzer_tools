import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from log_analyzer.extractor import CancelToken, Cancelled
from log_analyzer.flat import scan_flat, scan_tree
from log_analyzer.scope import for_folder_tree
from log_analyzer.search import SearchOptions, search
from log_analyzer.source import FlatSource
from tests.fixtures import build_zip, write


class _TreeFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='la-flat-')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, data):
        path = os.path.join(self.tmp, *rel.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)
        return path

    def snapshot(self):
        """Every path under the tree, so a test can prove nothing appeared."""
        out = []
        for root, dirs, files in os.walk(self.tmp):
            dirs.sort()
            for name in dirs + files:
                out.append(os.path.join(root, name))
        return sorted(out)


class FlatScanTest(_TreeFixture):
    def test_scan_enumerates_every_file_recursively(self):
        self.write('top.log', b'one')
        self.write('a/b/c.log', b'two')
        man = scan_flat(self.tmp)
        self.assertEqual(sorted(e.vp for e in man.entries), ['a/b/c.log', 'top.log'])

    def test_vp_uses_forward_slashes_and_feed_name_works(self):
        self.write('a/b/c.log', b'x')
        entry = scan_flat(self.tmp).entries[0]
        self.assertEqual(entry.vp, 'a/b/c.log')
        # vp always uses '/', never the native separator: os.sep is '\\' on
        # Windows and '/' on POSIX, so only the explicit backslash is stable.
        self.assertNotIn('\\', entry.vp)
        # FileEntry.name splits on '/', so this would break on a Windows path.
        self.assertEqual(entry.name, 'c.log')

    def test_disk_is_relative_to_the_root_and_joins_back(self):
        path = self.write('a/b/c.log', b'x')
        entry = scan_flat(self.tmp).entries[0]
        self.assertFalse(os.path.isabs(entry.disk))
        self.assertEqual(os.path.join(self.tmp, entry.disk), path)

    def test_single_file_yields_one_entry_named_by_basename(self):
        path = self.write('a/only.log', b'x')
        man = scan_flat(path)
        self.assertEqual(len(man.entries), 1)
        self.assertEqual(man.entries[0].vp, 'only.log')
        self.assertEqual(man.entries[0].disk, 'only.log')

    def test_no_extension_filter(self):
        # Filtering would silently hide oddly named logs; binary content is
        # already handled at search time by the Hide binary checkbox.
        for name in ('a.log', 'b.dat', 'c.bin', 'noext'):
            self.write(name, b'x')
        self.assertEqual(len(scan_flat(self.tmp).entries), 4)

    def test_nothing_is_written_to_disk(self):
        # The whole point of in-place search: no copy, no cache, no manifest.
        self.write('a/b/c.log', b'x')
        before = self.snapshot()
        scan_flat(self.tmp)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'archives')))

    def test_manifest_is_complete_with_accurate_stats(self):
        self.write('a/b/c.log', b'12345')
        self.write('top.log', b'123')
        man = scan_flat(self.tmp)
        self.assertEqual(man.status, 'complete')
        self.assertEqual(man.stats['files'], 2)
        self.assertEqual(man.stats['total_bytes'], 8)
        self.assertEqual(man.archive_name, os.path.abspath(self.tmp))

    def test_ordering_is_deterministic(self):
        for name in ('z.log', 'a.log', 'm.log'):
            self.write(name, b'x')
        self.write('d1/x.log', b'x')
        self.write('d2/x.log', b'x')
        first = [e.vp for e in scan_flat(self.tmp).entries]
        second = [e.vp for e in scan_flat(self.tmp).entries]
        self.assertEqual(first, second)
        # Sorted within each directory (os.walk visits a directory's files
        # before descending), which is the guarantee that matters: the same
        # tree lists in the same order every run.
        self.assertEqual(first, ['a.log', 'm.log', 'z.log',
                                 'd1/x.log', 'd2/x.log'])

    def test_entry_cap_stops_the_walk_and_warns(self):
        for i in range(5):
            self.write('f%d.log' % i, b'x')
        with patch('log_analyzer.flat.MAX_FLAT_ENTRIES', 3):
            man = scan_flat(self.tmp)
        self.assertEqual(len(man.entries), 3)
        self.assertTrue(any('larger than' in w for w in man.warnings))

    def test_cancel_stops_the_scan(self):
        for i in range(10):
            self.write('f%d.log' % i, b'x')
        cancel = CancelToken()
        cancel.cancel()
        with self.assertRaises(Cancelled):
            scan_flat(self.tmp, cancel=cancel)

    def test_an_unreadable_root_becomes_a_warning_not_a_crash(self):
        # os.walk routes a failed readdir here; the scan must come back with
        # an empty listing and a warning instead of propagating OSError.
        man = scan_flat(os.path.join(self.tmp, 'does-not-exist'))
        self.assertEqual(man.entries, [])
        self.assertTrue(man.warnings)

    def test_a_vanished_file_is_skipped_with_a_warning(self):
        self.write('a/x.log', b'x')
        man = scan_flat(self.tmp)
        self.assertEqual(len(man.entries), 1)
        # stat() on a path that no longer exists is the same code path as a
        # permission failure, and both must degrade to a warning.
        with patch('log_analyzer.flat.os.stat', side_effect=OSError(13, 'denied')):
            man = scan_flat(self.tmp)
        self.assertEqual(man.entries, [])
        self.assertTrue(any('cannot stat' in w for w in man.warnings))

    def test_progress_is_reported_for_large_trees(self):
        for i in range(6):
            self.write('f%d.log' % i, b'x')
        seen = []
        with patch('log_analyzer.flat.PROGRESS_EVERY', 2):
            scan_flat(self.tmp, progress=lambda *a: seen.append(a))
        self.assertTrue(seen)

    @unittest.skipIf(os.name == 'nt', 'symlinks need elevation on Windows')
    def test_symlink_loop_does_not_hang(self):
        self.write('a/x.log', b'x')
        os.symlink(os.path.join(self.tmp, 'a'), os.path.join(self.tmp, 'a', 'link'))
        man = scan_flat(self.tmp)
        self.assertEqual(len(man.entries), 1)

    def test_a_tree_containing_the_workspace_is_flagged(self):
        self.write('a/x.log', b'x')
        man = scan_flat(self.tmp, ws_root=os.path.join(self.tmp, 'a'))
        self.assertTrue(any('workspace' in w for w in man.warnings))


class FlatSourceTest(_TreeFixture):
    def test_open_returns_the_in_place_bytes(self):
        self.write('a/b/c.log', b'hello')
        entry = scan_flat(self.tmp).entries[0]
        fh = FlatSource(self.tmp).open(entry)
        try:
            self.assertEqual(fh.read(), b'hello')
            fh.seek(0)
            self.assertEqual(fh.read(2), b'he')
        finally:
            fh.close()

    def test_single_file_scope_opens_through_the_parent_base(self):
        path = self.write('a/only.log', b'hi')
        man = scan_flat(path)
        fh = FlatSource(os.path.dirname(path)).open(man.entries[0])
        try:
            self.assertEqual(fh.read(), b'hi')
        finally:
            fh.close()

    def test_entries_are_searchable_through_the_one_method_protocol(self):
        # Proves the search core needs nothing but open(entry): no key, no
        # workspace, no manifest on disk.
        self.write('a/hit.log', b'aaa\nsub_temp is 42\n')
        self.write('b/miss.log', b'bbb\n')
        man = scan_flat(self.tmp)
        got = []
        summary = search(man.entries, FlatSource(self.tmp),
                         SearchOptions('sub_temp'),
                         cancel=CancelToken(), sink=got.extend)
        self.assertEqual(summary.matched_files, 1)
        self.assertEqual(got[0].entry.vp, 'a/hit.log')


if __name__ == '__main__':
    unittest.main()


class ScanTreeTest(_TreeFixture):
    """A folder that may itself contain archives.

    The point of scan_tree over scan_flat: an archive inside the folder must
    contribute its *contents*, not its compressed bytes. Before this, a folder
    search listed the .zip itself as a hit and never read what was inside it.
    """

    def setUp(self):
        super().setUp()
        self.ws = tempfile.mkdtemp(prefix='la-flat-ws-')

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)
        super().tearDown()

    def test_loose_files_and_archive_members_share_one_entry_list(self):
        self.write('top.log', b'NEEDLE top\n')
        self.write('sub/mid.log', b'NEEDLE mid\n')
        self.write('sub/deep/low.log', b'NEEDLE low\n')
        write(self.tmp + '/sub/bundle.zip',
              build_zip({'inside/d.log': b'NEEDLE zipped\n'}))

        man, keys = scan_tree(self.tmp, self.ws)
        vps = [e.vp for e in man.entries]
        self.assertIn('top.log', vps)
        # Recursion down the tree is the part that must not regress.
        self.assertIn('sub/deep/low.log', vps)
        # The archive's member, named by the archive it came from.
        self.assertIn('sub/bundle.zip/inside/d.log', vps)
        # And the archive itself is not a leaf any more.
        self.assertNotIn('sub/bundle.zip', vps)
        self.assertEqual(len(keys), 1)

    def test_archive_members_are_searchable_through_the_scope_source(self):
        self.write('loose.log', b'NEEDLE loose\n')
        write(self.tmp + '/b.zip', build_zip({'in.log': b'NEEDLE inside\n'}))
        man, keys = scan_tree(self.tmp, self.ws)
        scope = for_folder_tree(self.tmp, man, self.ws, keys)

        got = []
        search(scope.entries, scope.source, SearchOptions('NEEDLE'),
               cancel=CancelToken(), sink=got.extend)
        self.assertEqual(sorted(f.entry.vp for f in got),
                         ['b.zip/in.log', 'loose.log'])

    def test_archive_contents_land_in_the_workspace_but_loose_files_do_not(self):
        self.write('loose.log', b'NEEDLE\n')
        write(self.tmp + '/b.zip', build_zip({'in.log': b'NEEDLE\n'}))
        before = set(self.snapshot())

        man, keys = scan_tree(self.tmp, self.ws)
        # Reading in place must not touch the source tree at all.
        self.assertEqual(set(self.snapshot()), before)
        # The archive did have to be extracted, under a namespaced key.
        self.assertTrue(os.path.isdir(os.path.join(self.ws, 'archives', keys[0])))
        self.assertTrue(keys[0].startswith('local-'))

    def test_a_second_scan_of_an_unchanged_folder_is_a_cache_hit(self):
        write(self.tmp + '/b.zip', build_zip({'in.log': b'NEEDLE\n'}))
        man1, keys1 = scan_tree(self.tmp, self.ws)
        stamp = os.path.getmtime(os.path.join(self.ws, 'archives', keys1[0]))

        man2, keys2 = scan_tree(self.tmp, self.ws)
        self.assertEqual(keys1, keys2)
        self.assertEqual([e.vp for e in man1.entries], [e.vp for e in man2.entries])
        self.assertEqual(
            stamp, os.path.getmtime(os.path.join(self.ws, 'archives', keys2[0])))

    def test_an_edited_archive_is_extracted_again(self):
        path = self.tmp + '/b.zip'
        write(path, build_zip({'in.log': b'first\n'}))
        _, keys1 = scan_tree(self.tmp, self.ws)
        # Same member name, different bytes and mtime: the key must move, or
        # the search would keep reporting the stale contents.
        write(path, build_zip({'in.log': b'a much longer second payload\n'}))
        man2, keys2 = scan_tree(self.tmp, self.ws)
        self.assertNotEqual(keys1, keys2)
        self.assertIn('b.zip/in.log', [e.vp for e in man2.entries])

    def test_deterministic_across_runs(self):
        self.write('z.log', b'x')
        self.write('a/m.log', b'x')
        write(self.tmp + '/b.zip', build_zip({'q.log': b'x', 'r.log': b'x'}))
        first = [e.vp for e in scan_tree(self.tmp, self.ws)[0].entries]
        second = [e.vp for e in scan_tree(self.tmp, self.ws)[0].entries]
        self.assertEqual(first, second)

    def test_a_corrupt_archive_is_a_warning_not_a_failure(self):
        self.write('good.log', b'NEEDLE\n')
        # PK magic so the sniff accepts it, then garbage: the container is
        # unreadable but the folder must still load.
        self.write('broken.zip', b'PK\x03\x04' + b'\x00' * 64)
        man, keys = scan_tree(self.tmp, self.ws)
        self.assertIn('good.log', [e.vp for e in man.entries])
        self.assertTrue(man.warnings)

    def test_returns_no_keys_when_there_are_no_archives(self):
        self.write('a.log', b'x')
        man, keys = scan_tree(self.tmp, self.ws)
        self.assertEqual(keys, ())
        self.assertEqual(man.stats['archives'], 0)
