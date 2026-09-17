import gzip
import os
import tarfile
import tempfile
import unittest
import zipfile

from log_analyzer.extractor import (
    CancelToken, Cancelled, MAX_DEPTH, extract_archive, scan_archive,
)
from log_analyzer.manifest import inspect_workspace, read_manifest
from log_analyzer.paths import archive_key
from tests.fixtures import build_zip, nested_zip, write


class ExtractTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='la-test-')
        self.ws = os.path.join(self.tmp, 'ws')

    def tearDown(self):
        from log_analyzer.paths import rmtree_robust
        rmtree_robust(self.tmp)

    def make_archive(self, name, data):
        p = os.path.join(self.tmp, name)
        write(p, data)
        return p

    def run_extract(self, path, **kw):
        key = archive_key(path)
        return extract_archive(path, self.ws, key, **kw), key


class NestedExtractionTest(ExtractTestBase):
    def test_four_levels_are_fully_expanded(self):
        # root -> L2 -> L3 -> L4 -> hilog/leaf.log
        leaf = b'deep content marker\n'
        l4 = build_zip({'hilog/leaf.log': leaf})
        l3 = build_zip({'l4.zip': l4})
        l2 = build_zip({'l3.zip': l3})
        root = build_zip({'CLUSTER/cluster/l2.zip': l2})
        p = self.make_archive('root.zip', root)

        man, _ = self.run_extract(p)
        vps = [e.vp for e in man.entries]
        self.assertEqual(len(man.entries), 1)
        self.assertEqual(
            vps[0],
            'root.zip/CLUSTER/cluster/l2.zip/l3.zip/l4.zip/hilog/leaf.log')
        self.assertEqual(man.status, 'complete')

    def test_every_leaf_is_materialised_and_readable(self):
        leaf = b'hello nested world\n'
        root = build_zip({'a/b.zip': build_zip({'c/d.zip': build_zip({'e.log': leaf})})})
        p = self.make_archive('root.zip', root)
        man, key = self.run_extract(p)
        from log_analyzer.source import DiskSource
        src = DiskSource(self.ws, key)
        with src.open(man.entries[0]) as fh:
            self.assertEqual(fh.read(), leaf)

    def test_intermediate_archives_are_not_kept_on_disk(self):
        # They are scaffolding; keeping them would cost ~200MB of dead weight.
        root = build_zip({'a.zip': build_zip({'b.zip': build_zip({'c.log': b'x'})})})
        p = self.make_archive('root.zip', root)
        man, key = self.run_extract(p)
        base = os.path.join(self.ws, 'archives', key, 'data')
        found = []
        for r, _d, fs in os.walk(base):
            found.extend(fs)
        self.assertEqual(len(found), 1, 'only the leaf should remain: %r' % found)

    def test_scan_matches_extract_counts(self):
        root = build_zip({'a.zip': build_zip({'x.log': b'1', 'y.log': b'2'}),
                          'b.zip': build_zip({'z.log': b'3'})})
        p = self.make_archive('root.zip', root)
        s = scan_archive(p)
        man, _ = self.run_extract(p)
        self.assertEqual(s.files, len(man.entries))


class SafetyTest(ExtractTestBase):
    def test_traversal_names_land_safely_under_data(self):
        root = build_zip({'../../evil.log': b'pwned',
                          'a/../../../evil2.log': b'pwned2'})
        p = self.make_archive('root.zip', root)
        man, key = self.run_extract(p)
        self.assertEqual(len(man.entries), 2)
        base = os.path.abspath(os.path.join(self.ws, 'archives', key))
        for e in man.entries:
            full = os.path.abspath(os.path.join(base, *e.disk.split('/')))
            self.assertTrue(full.startswith(base), 'escaped workspace: %s' % full)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'evil.log')))

    def test_absolute_member_name_is_contained(self):
        root = build_zip({'/etc/evil.log': b'x'})
        p = self.make_archive('root.zip', root)
        man, key = self.run_extract(p)
        self.assertEqual(len(man.entries), 1)
        self.assertTrue(man.entries[0].disk.startswith('data/'))

    def test_deep_nesting_is_capped(self):
        payload = build_zip({'leaf.log': b'x'})
        for i in range(MAX_DEPTH + 4):
            payload = build_zip({'d%d.zip' % i: payload})
        p = self.make_archive('root.zip', payload)
        man, _ = self.run_extract(p)
        self.assertEqual(man.status, 'complete')
        self.assertTrue(any('max depth' in w for w in man.warnings))

    def test_high_ratio_member_is_skipped(self):
        import zlib
        blob = b'\x00' * (4 << 20)
        comp = zlib.compressobj(9, zlib.DEFLATED, -15)
        cdata = comp.compress(blob) + comp.flush()
        self.assertLess(len(blob) / len(cdata), 100000)  # sanity
        # A zip whose member expands far beyond the ratio limit.
        root = build_zip({'bomb.bin': blob, 'ok.log': b'fine'}, compression=zipfile.ZIP_DEFLATED)
        p = self.make_archive('root.zip', root)
        s = scan_archive(p)
        self.assertTrue(any('ratio' in w for w in s.warnings), s.warnings)

    def test_corrupt_nested_archive_does_not_abort_siblings(self):
        root = build_zip({
            'bad.zip': b'PK\x03\x04 not really a zip at all',
            'good.zip': build_zip({'survivor.log': b'found me'}),
        })
        p = self.make_archive('root.zip', root)
        man, _ = self.run_extract(p)
        vps = [e.vp for e in man.entries]
        self.assertTrue(any('survivor.log' in v for v in vps), vps)
        self.assertTrue(man.warnings)

    def test_not_an_archive_raises_valueerror(self):
        p = self.make_archive('plain.bin', b'this is just text, not an archive')
        with self.assertRaises(ValueError):
            scan_archive(p)


class CancellationTest(ExtractTestBase):
    def test_cancel_produces_partial_manifest_not_complete(self):
        inner = build_zip({'%03d.log' % i: b'x' * 200 for i in range(30)})
        root = build_zip({'a.zip': inner, 'b.zip': build_zip({'y.log': b'2'})})
        p = self.make_archive('root.zip', root)
        tok = CancelToken()
        calls = {'n': 0}

        def prog(phase, done, total, note):
            calls['n'] += 1
            if calls['n'] >= 1:
                tok.cancel()

        man, key = self.run_extract(p, cancel=tok, progress=prog)
        self.assertEqual(man.status, 'partial')
        state, _, entries, done = inspect_workspace(self.ws, key)
        self.assertEqual(state, 'partial')
        self.assertGreater(len(done), 0)


class ResumeTest(ExtractTestBase):
    def test_resume_completes_and_marks_complete(self):
        inner_a = build_zip({'%03d.log' % i: b'x' for i in range(5)})
        root = build_zip({'a.zip': inner_a, 'b.zip': build_zip({'y.log': b'2'})})
        p = self.make_archive('root.zip', root)
        key = archive_key(p)
        tok = CancelToken()
        calls = {'n': 0}

        def prog(phase, done, total, note):
            calls['n'] += 1
            tok.cancel()

        man1 = extract_archive(p, self.ws, key, cancel=tok, progress=prog)
        self.assertEqual(man1.status, 'partial')
        done_before = len(man1.entries)

        man2 = extract_archive(p, self.ws, key, resume=True)
        self.assertEqual(man2.status, 'complete')
        self.assertGreater(len(man2.entries), done_before)
        vps = [e.vp for e in man2.entries]
        self.assertEqual(len(vps), len(set(vps)), 'resume duplicated entries')

    def test_cached_complete_run_is_reused(self):
        p = self.make_archive('root.zip', build_zip({'a.log': b'x'}))
        man1, key = self.run_extract(p)
        man2 = extract_archive(p, self.ws, key)
        self.assertEqual(len(man1.entries), len(man2.entries))
        self.assertEqual(man2.status, 'complete')


class GzipTest(ExtractTestBase):
    def test_tar_gz_branch(self):
        import io
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as t:
            data = b'tarred log line\n'
            info = tarfile.TarInfo('logs/inner.log')
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        root = build_zip({'bundle.tar.gz': buf.getvalue()})
        p = self.make_archive('root.zip', root)
        man, _ = self.run_extract(p)
        self.assertTrue(any('inner.log' in e.vp for e in man.entries),
                        [e.vp for e in man.entries])


class BudgetTest(ExtractTestBase):
    """A budget stop must never masquerade as a complete extraction.

    MAX_ENTRIES/MAX_TOTAL_BYTES stop the walk mid-way, so the run covers only
    part of the archive. Reporting 'complete' would both lie about coverage and
    delete manifest.partial.jsonl -- the only record of what did finish.
    """

    def _many_tops(self, n=6):
        inner = build_zip({'x.log': b'hello world'})
        return build_zip({'l2_%d.zip' % i: inner for i in range(n)})

    def test_budget_stop_reports_partial_not_complete(self):
        from unittest.mock import patch
        p = self.make_archive('root.zip', self._many_tops())
        key = archive_key(p)
        st = scan_archive(p)
        with patch('log_analyzer.extractor.MAX_ENTRIES', 2):
            man = extract_archive(p, self.ws, key, structural=st, force=True)
        self.assertEqual(man.status, 'partial')
        self.assertLess(len(man.entries), 6, 'budget did not actually bite')
        self.assertIsNone(read_manifest(self.ws, key),
                          'a budget stop wrote a complete manifest')
        self.assertEqual(inspect_workspace(self.ws, key)[0], 'partial')

    def test_resume_after_budget_stop_recovers_every_file(self):
        from unittest.mock import patch
        p = self.make_archive('root.zip', self._many_tops())
        key = archive_key(p)
        st = scan_archive(p)
        with patch('log_analyzer.extractor.MAX_ENTRIES', 2):
            extract_archive(p, self.ws, key, structural=st, force=True)
        # Budget lifted: the entry that stopped mid-way must be redone in full,
        # not skipped as 'already done' (which would lose its files silently).
        man = extract_archive(p, self.ws, key, resume=True, structural=st)
        self.assertEqual(man.status, 'complete')
        self.assertEqual(len(man.entries), 6)
        vps = [e.vp for e in man.entries]
        self.assertEqual(len(vps), len(set(vps)), 'resume duplicated entries')
        self.assertEqual(len(set(e.id for e in man.entries)), 6,
                         'resume reused a sequence id')


if __name__ == '__main__':
    unittest.main()
