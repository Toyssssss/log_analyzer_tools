import json
import os
import tempfile
import unittest

from log_analyzer.manifest import (
    FileEntry, Manifest, STATUS_COMPLETE, STATUS_PARTIAL, append_partial,
    begin_extraction, build_partial_manifest, clear_workspace, index_remove,
    index_upsert, inspect_workspace, load_index, make_ref, read_manifest,
    scan_workspace, write_manifest,
)
from log_analyzer.extractor import CancelToken
from log_analyzer.manifest import ScanSummary
from log_analyzer.paths import archive_key, atomic_write_json, human_bytes


class WorkspaceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='la-man-')
        self.ws = os.path.join(self.tmp, 'ws')
        self.key = 'abc123def456abcd'

    def tearDown(self):
        from log_analyzer.paths import rmtree_robust
        rmtree_robust(self.tmp)

    def entry(self, i, vp='root.zip/a.log'):
        return FileEntry(id=i, vp=vp, disk='data/00/%08x' % i, size=10, depth=0)


class ManifestIOTest(WorkspaceTestBase):
    def test_round_trip(self):
        man = Manifest(status=STATUS_COMPLETE, archive_name='root.zip',
                       archive_path='D:/root.zip', archive_size=1,
                       archive_mtime_ns=2, stats={'files': 1, 'total_bytes': 3},
                       entries=[self.entry(0)])
        write_manifest(self.ws, self.key, man)
        back = read_manifest(self.ws, self.key)
        self.assertIsNotNone(back)
        self.assertEqual(back.status, 'complete')
        self.assertEqual(back.archive_name, 'root.zip')
        self.assertEqual(back.entries[0].vp, 'root.zip/a.log')
        self.assertEqual(back.entries[0].disk, 'data/00/00000000')

    def test_write_removes_marker_and_jsonl(self):
        begin_extraction(self.ws, self.key)
        append_partial(self.ws, self.key, [self.entry(0)], 0)
        state, _, _, _ = inspect_workspace(self.ws, self.key)
        self.assertEqual(state, 'partial')
        write_manifest(self.ws, self.key,
                       Manifest(status=STATUS_COMPLETE, archive_name='r.zip'))
        state, man, _, _ = inspect_workspace(self.ws, self.key)
        self.assertEqual(state, 'complete')

    def test_partial_jsonl_round_trip(self):
        begin_extraction(self.ws, self.key)
        append_partial(self.ws, self.key, [self.entry(0), self.entry(1)], 0)
        append_partial(self.ws, self.key, [self.entry(2)], 1)
        state, man, entries, done = inspect_workspace(self.ws, self.key)
        self.assertEqual(state, 'partial')
        self.assertIsNone(man)
        self.assertEqual(len(entries), 3)
        self.assertEqual(done, {0, 1})

    def test_torn_final_line_is_discarded(self):
        begin_extraction(self.ws, self.key)
        append_partial(self.ws, self.key, [self.entry(0)], 0)
        # Simulate a power loss mid-write: append a half-written JSON line.
        p = os.path.join(self.ws, 'archives', self.key, 'manifest.partial.jsonl')
        with open(p, 'a', encoding='utf-8') as f:
            f.write('{"top_index": 1, "entr')
        state, _, entries, done = inspect_workspace(self.ws, self.key)
        self.assertEqual(state, 'partial')
        self.assertEqual(done, {0})
        self.assertEqual(len(entries), 1)

    def test_absent_when_nothing_on_disk(self):
        state, man, entries, done = inspect_workspace(self.ws, self.key)
        self.assertEqual(state, 'absent')
        self.assertEqual(entries, [])

    def test_marker_without_jsonl_is_partial_not_complete(self):
        begin_extraction(self.ws, self.key)
        state, _, _, _ = inspect_workspace(self.ws, self.key)
        self.assertEqual(state, 'partial')

    def test_marker_plus_manifest_is_complete(self):
        # Atomic replace succeeded but cleanup was interrupted.
        write_manifest(self.ws, self.key,
                       Manifest(status=STATUS_COMPLETE, archive_name='r.zip'))
        from log_analyzer.paths import atomic_write_json
        atomic_write_json(
            os.path.join(self.ws, 'archives', self.key, 'EXTRACTING'), {'pid': 1})
        state, _, _, _ = inspect_workspace(self.ws, self.key)
        self.assertEqual(state, 'complete')


class IndexTest(WorkspaceTestBase):
    def test_upsert_and_load(self):
        man = Manifest(status=STATUS_COMPLETE, archive_name='a.zip',
                       stats={'files': 1, 'total_bytes': 5})
        index_upsert(self.ws, make_ref(self.key, man))
        idx = load_index(self.ws)
        self.assertEqual(len(idx['archives']), 1)
        self.assertEqual(idx['archives'][0]['display_name'], 'a.zip')

    def test_upsert_replaces_same_key(self):
        for name in ('a.zip', 'b.zip'):
            man = Manifest(archive_name=name)
            index_upsert(self.ws, make_ref(self.key, man))
        self.assertEqual(len(load_index(self.ws)['archives']), 1)

    def test_remove(self):
        index_upsert(self.ws, make_ref(self.key, Manifest(archive_name='a.zip')))
        index_remove(self.ws, self.key)
        self.assertEqual(load_index(self.ws)['archives'], [])

    def test_scan_workspace_drops_missing_dirs(self):
        index_upsert(self.ws, make_ref(self.key, Manifest(archive_name='a.zip')))
        info = scan_workspace(self.ws)
        self.assertEqual(info['archives'], [], 'ghost row should be dropped')

    def test_scan_workspace_reports_real_bytes(self):
        from log_analyzer.paths import ensure_dir
        d = os.path.join(self.ws, 'archives', self.key, 'data', '00')
        ensure_dir(d)
        with open(os.path.join(d, '00000000'), 'wb') as f:
            f.write(b'1234567890')
        index_upsert(self.ws, make_ref(self.key, Manifest(archive_name='a.zip')))
        info = scan_workspace(self.ws)
        self.assertEqual(info['total_bytes'], 10)

    def test_clear_one_archive_only(self):
        from log_analyzer.paths import ensure_dir
        other = 'ffffffffffffffff'
        for k in (self.key, other):
            ensure_dir(os.path.join(self.ws, 'archives', k, 'data'))
            index_upsert(self.ws, make_ref(k, Manifest(archive_name=k)))
        clear_workspace(self.ws, self.key)
        self.assertEqual([a['key'] for a in load_index(self.ws)['archives']], [other])

    def test_clear_all(self):
        from log_analyzer.paths import ensure_dir
        ensure_dir(os.path.join(self.ws, 'archives', self.key, 'data'))
        index_upsert(self.ws, make_ref(self.key, Manifest(archive_name='a.zip')))
        clear_workspace(self.ws)
        self.assertEqual(load_index(self.ws)['archives'], [])


class PartialManifestTest(WorkspaceTestBase):
    def test_recovered_manifest_never_claims_complete(self):
        entries = [self.entry(0), self.entry(1)]
        man = build_partial_manifest(self.ws, self.key, 'root.zip', entries, {0},
                                     ScanSummary(top_level_entries=9))
        self.assertEqual(man.status, 'partial')
        self.assertNotEqual(man.status, STATUS_COMPLETE)
        self.assertEqual(man.stats['completed_entries'], 1)
        self.assertEqual(man.stats['top_level_entries'], 9)


class AtomicityTest(WorkspaceTestBase):
    def test_atomic_write_leaves_no_temp_files(self):
        p = os.path.join(self.ws, 'x.json')
        atomic_write_json(p, {'a': 1})
        atomic_write_json(p, {'a': 2})
        self.assertEqual([f for f in os.listdir(self.ws) if f.startswith('.tmp-')], [])

    def test_read_json_returns_default_on_corrupt(self):
        from log_analyzer.paths import read_json
        p = os.path.join(self.ws, 'bad.json')
        os.makedirs(self.ws, exist_ok=True)
        with open(p, 'w') as f:
            f.write('{not json')
        self.assertEqual(read_json(p, 'fallback'), 'fallback')


class HumanBytesTest(unittest.TestCase):
    def test_scales(self):
        self.assertEqual(human_bytes(512), '512 B')
        self.assertEqual(human_bytes(2048), '2.0 KB')
        self.assertEqual(human_bytes(2.25 * 1024 ** 3), '2.2 GB')


if __name__ == '__main__':
    unittest.main()
