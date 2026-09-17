import gc
import os
import tempfile
import unittest

import tkinter as tk

from log_analyzer.manifest import FileEntry, Manifest
from log_analyzer.paths import archive_key
from log_analyzer.search import FileHits, Hit
from tests.fixtures import build_zip, write


def _tk_available():
    try:
        r = tk.Tk()
        r.destroy()
        return True
    except Exception:
        return False


HAVE_TK = _tk_available()


@unittest.skipUnless(HAVE_TK, 'no display available')
class ResultTreeTest(unittest.TestCase):
    def setUp(self):
        from log_analyzer.gui.results_view import ResultTree
        self.root = tk.Tk()
        self.root.withdraw()
        self.tree = ResultTree(self.root)

    def tearDown(self):
        # See AppFlowTest.tearDown: release the widget tree before the Tk
        # interpreter goes away.
        self.tree = None
        gc.collect()
        self.root.destroy()

    def _fh(self, vp, hits, total=None, truncated=False, error=None, binary=False):
        e = FileEntry(id=0, vp=vp, disk='data/00/00000000', size=1, depth=0)
        return FileHits(e, [Hit(n, t) for n, t in hits],
                        total if total is not None else len(hits),
                        truncated, 'utf-8', binary, error=error)

    def test_file_rows_only_until_expanded(self):
        # The whole point of lazy expansion: inserting a file must NOT insert
        # its hit lines yet.
        fh = self._fh('a.zip/x.log', [(1, 'one'), (2, 'two'), (3, 'three')])
        self.tree.append_batch([fh])
        self.assertEqual(self.tree.file_count(), 1)
        iid = self.tree.tree.get_children('')[0]
        self.assertEqual(self.tree.tree.get_children(iid), (),
                         'children must not exist before expansion')

    def test_expand_materialises_all_lines_once(self):
        fh = self._fh('a.zip/x.log', [(i, 'line %d' % i) for i in range(1, 6)])
        self.tree.append_batch([fh])
        iid = self.tree.tree.get_children('')[0]
        self.tree._materialise(iid)
        self.assertEqual(len(self.tree.tree.get_children(iid)), 5)
        self.tree._materialise(iid)      # idempotent
        self.assertEqual(len(self.tree.tree.get_children(iid)), 5)

    def test_collapse_hides_but_keeps_lines(self):
        fh = self._fh('a.zip/x.log', [(1, 'one')])
        self.tree.append_batch([fh])
        iid = self.tree.tree.get_children('')[0]
        self.tree.expand_all()
        self.tree.collapse_all()
        self.assertFalse(self.tree.tree.item(iid, 'open'))
        self.assertEqual(len(self.tree.tree.get_children(iid)), 1)

    def test_truncated_count_is_shown(self):
        fh = self._fh('a.zip/x.log', [(1, 'one')], total=999, truncated=True)
        self.tree.append_batch([fh])
        iid = self.tree.tree.get_children('')[0]
        self.assertIn('999', self.tree.tree.item(iid, 'values')[1])

    def test_error_row_marked(self):
        fh = self._fh('a.zip/x.log', [], error='boom')
        self.tree.append_batch([fh])
        iid = self.tree.tree.get_children('')[0]
        self.assertEqual(self.tree.tree.item(iid, 'values')[1], 'error')

    def test_clear_removes_everything(self):
        self.tree.append_batch([self._fh('a.zip/x.log', [(1, 'one')])])
        self.tree.clear()
        self.assertEqual(self.tree.file_count(), 0)
        self.assertEqual(self.tree.tree.get_children(''), ())

    def test_selection_callback_fires(self):
        seen = []
        self.tree.on_select = seen.append
        self.tree.append_batch([self._fh('a.zip/x.log', [(1, 'one')])])
        iid = self.tree.tree.get_children('')[0]
        self.tree.tree.selection_set(iid)
        self.tree._on_select(None)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].entry.vp, 'a.zip/x.log')


@unittest.skipUnless(HAVE_TK, 'no display available')
class AppFlowTest(unittest.TestCase):
    """End-to-end through the real widgets, driving the worker by pumping
    the Tk event loop instead of waiting on a human."""

    def setUp(self):
        from log_analyzer.gui.app import App
        self.tmp = tempfile.mkdtemp(prefix='la-gui-')
        self.ws = os.path.join(self.tmp, 'ws')
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, workspace=self.ws)

    def tearDown(self):
        from log_analyzer.paths import rmtree_robust
        # Drop the App (and the StringVars it owns) while the interpreter is
        # still alive. Otherwise they are collected during a later test and
        # their __del__ calls into a destroyed Tk, dumping 'main thread is not
        # in main loop' tracebacks that look like test failures.
        self.app = None
        gc.collect()
        try:
            self.root.destroy()
        except Exception:
            pass
        rmtree_robust(self.tmp)

    def pump(self, seconds=20):
        import time
        end = time.time() + seconds
        while time.time() < end:
            self.root.update()
            if self.app.job is None and self.app.manifest is not None:
                return True
            time.sleep(0.01)
        return False

    def test_extract_then_search_through_the_widgets(self):
        leaf = b'08-28 00:23:09.180 506 522 I TAG: sub_temp is 42\n'
        inner = build_zip({'hilog/a.log': leaf, 'hilog/b.log': b'no match\n'})
        root_zip = build_zip({'CLUSTER/cluster/l2.zip': inner})
        path = os.path.join(self.tmp, 'Log_X.zip')
        write(path, root_zip)

        # Bypass the modal preflight and extract straight through the engine,
        # then exercise the search half through the real widgets.
        from log_analyzer.extractor import extract_archive
        key = archive_key(path)
        man = extract_archive(path, self.ws, key)
        self.app.key = key
        self.app.manifest = man

        self.app.kw_var.set('sub_temp')
        self.app.on_search()
        self.assertTrue(self.pump(), 'search did not finish')

        self.assertEqual(self.app.results.file_count(), 1)
        iid = self.app.results.tree.get_children('')[0]
        self.app.results._materialise(iid)
        self.assertEqual(len(self.app.results.tree.get_children(iid)), 1)
        self.assertIn('1 hits in 1 files', self.app.status_var.get())

    def test_whole_word_checkbox_reaches_the_pattern(self):
        leaf = b'sub_temp_extra\nsub_temp was here\n'
        path = os.path.join(self.tmp, 'Log_Y.zip')
        write(path, build_zip({'a.log': leaf}))
        from log_analyzer.extractor import extract_archive
        key = archive_key(path)
        man = extract_archive(path, self.ws, key)
        self.app.key = key
        self.app.manifest = man

        self.app.kw_var.set('sub_temp')
        self.app.word_var.set(True)
        self.app.on_search()
        self.assertTrue(self.pump())
        iid = self.app.results.tree.get_children('')[0]
        fh = self.app.results._files[iid]
        self.assertEqual(fh.total_hits, 1)
        self.assertIn('was here', fh.hits[0].line)

    def test_binary_filter_checkbox(self):
        path = os.path.join(self.tmp, 'Log_Z.zip')
        write(path, build_zip({'bin.dat': b'\x00\x01 sub_temp \x00'}))
        from log_analyzer.extractor import extract_archive
        key = archive_key(path)
        man = extract_archive(path, self.ws, key)
        self.app.key = key
        self.app.manifest = man

        self.app.kw_var.set('sub_temp')
        self.app.binary_var.set(True)
        self.app.on_search()
        self.assertTrue(self.pump())
        self.assertEqual(self.app.results.file_count(), 0)


@unittest.skipUnless(HAVE_TK, 'no display available')
class DetailPaneTest(unittest.TestCase):
    """The detail pane: whole-file view, filter, and highlighting."""

    def setUp(self):
        from log_analyzer.gui.detail_view import DetailPane
        self.root = tk.Tk()
        self.root.withdraw()
        self.lines = [
            '08-28 00:23:09.180  506  522 I TAG: msg is sub_temp',
            '08-28 00:23:09.181  506  522 I TAG: other line',
            '08-28 00:23:09.182  3171  3171 I testApp: hello',
            '08-28 00:23:09.183  506  522 W TAG: sub_temp again',
        ]
        self.pane = DetailPane(self.root, loader=self._load)
        self.pane.pack(fill='both', expand=True)

    def _load(self, entry):
        return list(self.lines), False

    def tearDown(self):
        self.pane = None
        gc.collect()
        try:
            self.root.destroy()
        except Exception:
            pass

    def _fh(self, hit_lines):
        from log_analyzer.manifest import FileEntry
        from log_analyzer.search import FileHits, Hit
        e = FileEntry(id=0, vp='a.zip/x.log', disk='data/00/00000000', size=1, depth=0)
        return FileHits(e, [Hit(n, self.lines[n - 1]) for n in hit_lines],
                        len(hit_lines), False, 'utf-8', False)

    def _shown(self):
        # ''.split('\n') is [''], which would make an empty view look like one
        # line; normalise that so count assertions mean what they say.
        raw = self.pane.text.get('1.0', 'end-1c')
        return raw.split('\n') if raw else []

    def test_matched_only_shows_just_the_hits(self):
        self.pane.show(self._fh([1, 4]), 'sub_temp')
        self.root.update()
        shown = self._shown()
        self.assertEqual(len(shown), 2)
        self.assertIn('sub_temp', shown[0])

    def test_all_text_mode_shows_every_line(self):
        self.pane.show(self._fh([1, 4]), 'sub_temp')
        self.root.update()
        self.pane.mode_var.set('all')
        self.pane._rerender()
        self.assertEqual(len(self._shown()), len(self.lines))

    def test_filter_adds_non_hit_lines_in_matched_mode(self):
        # Hits are lines 1 and 4; '3171' matches only line 3, which is not a
        # hit. The union shows all three, so filtering inside the file widens
        # the view instead of emptying it.
        self.pane.show(self._fh([1, 4]), 'sub_temp')
        self.root.update()
        self.pane.filter_var.set('3171')
        self.pane._rerender()
        shown = self._shown()
        self.assertEqual(len(shown), 3)
        # Document order: the hits (1, 4) with the filter's line 3 between them,
        # not appended at the end -- this is a log, and reordering it would
        # break the reading of a call sequence.
        self.assertIn('testApp', shown[1])
        self.assertIn('+1 non-hit lines', self.pane.info_var.get())

    def test_multiple_terms_are_ored(self):
        self.pane.show(self._fh([1, 4]), 'sub_temp')
        self.root.update()
        self.pane.filter_var.set('3171 08-28')
        self.pane._rerender()
        # 3171 adds the testApp line; '08-28' appears in every line, so the
        # union is the whole file.
        self.assertEqual(len(self._shown()), 4)

    def test_or_semantics_do_not_require_all_terms(self):
        self.pane.show(self._fh([3]), 'sub_temp')
        self.root.update()
        self.pane.filter_var.set('3171 heLLo')
        self.pane._rerender()
        shown = self._shown()
        self.assertEqual(len(shown), 1, 'OR should need only one term')
        self.assertIn('testApp', shown[0])

    def test_all_text_mode_ignores_the_filter(self):
        # Switching to 'All file text' must reveal the whole file even with a
        # filter typed in; the filter only highlights there.
        self.pane.show(self._fh([1, 4]), 'sub_temp')
        self.root.update()
        self.pane.filter_var.set('3171')
        self.pane._rerender()
        # Union: the filter adds its non-hit line, so the matched-only view is
        # the whole file here. It is the mode switch, not the filter, that
        # decides whether the rest is hidden.
        self.assertEqual(len(self._shown()), 3)
        self.pane.mode_var.set('all')
        self.pane._rerender()
        self.assertEqual(len(self._shown()), len(self.lines))
        self.assertIn('highlight only', self.pane.info_var.get())

    def test_highlight_tag_covers_the_keyword(self):
        self.pane.show(self._fh([1]), 'sub_temp')
        self.root.update()
        ranges = self.pane.text.tag_ranges('hl')
        self.assertTrue(ranges, 'no highlight applied')
        start = self.pane.text.index(ranges[0])
        end = self.pane.text.index(ranges[1])
        self.assertEqual(self.pane.text.get(start, end), 'sub_temp')

    def test_highlight_uses_light_green(self):
        self.assertEqual(str(self.pane.text.tag_cget('hl', 'background')), '#ccffcc')

    def test_clear_filter_restores_the_base_set(self):
        self.pane.show(self._fh([1, 4]), 'sub_temp')
        self.root.update()
        self.pane.filter_var.set('3171')
        self.pane._rerender()
        self.pane._clear_filter()
        self.assertEqual(len(self._shown()), 2)

    def test_selecting_another_file_reloads_content(self):
        self.pane.show(self._fh([1]), 'sub_temp')
        self.root.update()
        first = self._shown()
        self.pane.show(self._fh([2]), 'sub_temp')
        self.root.update()
        second = self._shown()
        self.assertNotEqual(first, second, 'pane did not reload the new file')

    def test_filter_persists_across_file_selection(self):
        # Investigating one pid across several files is the common case, so
        # the filter deliberately survives a row change -- the same way the
        # top-level keyword box does. The box still shows the term, so the
        # resulting (possibly empty) view is explainable rather than a
        # mystery.
        self.pane.show(self._fh([1, 2, 3, 4]), 'sub_temp')
        self.root.update()
        self.pane.mode_var.set('all')
        self.pane.filter_var.set('3171')
        self.pane._rerender()
        # Union, then narrowed by the mode: the hits 1-4 gain nothing from the
        # filter's line 3, so 'matched only' shows just the four hits.
        self.assertEqual(len(self._shown()), 4)
        self.pane.show(self._fh([1, 2, 3, 4]), 'sub_temp')
        self.root.update()
        self.assertEqual(self.pane.filter_var.get(), '3171')
        self.assertEqual(len(self._shown()), 4)

    def test_filter_cannot_empty_the_view(self):
        # Under union semantics a filter can only ever add lines to the hit
        # set, so a term that matches nothing leaves the hits untouched. The
        # earlier design let this empty the view, which read as a bug.
        self.pane.show(self._fh([1, 2]), 'sub_temp')
        self.root.update()
        self.pane.filter_var.set('no-such-term-anywhere')
        self.pane._rerender()
        self.assertEqual(len(self._shown()), 2)
        self.assertIn('showing 2 of', self.pane.info_var.get())
        self.assertIn('+0 non-hit lines', self.pane.info_var.get())

    def test_capped_hit_list_is_labelled(self):
        # search stops at max_hits_per_file; the pane must not imply it holds
        # every hit when it only holds the first batch.
        from log_analyzer.manifest import FileEntry
        from log_analyzer.search import FileHits, Hit
        e = FileEntry(id=0, vp='a.zip/x.log', disk='data/00/00000000', size=1, depth=0)
        fh = FileHits(e, [Hit(1, self.lines[0])], 900, True, 'utf-8', False)
        self.pane.show(fh, 'sub_temp')
        self.root.update()
        self.assertEqual(len(self._shown()), 1)
        self.assertIn('900 matched lines', self.pane.info_var.get())
        self.assertIn('first 1 listed', self.pane.info_var.get())

    def test_singular_hit_count_is_not_pluralised(self):
        self.pane.show(self._fh([1]), 'sub_temp')
        self.root.update()
        self.assertIn('1 matched line', self.pane.info_var.get())
        self.assertNotIn('1 matched lines', self.pane.info_var.get())

    def test_whole_word_highlight_excludes_partial_matches(self):
        # 'sub' is a prefix of 'sub_temp', so whole-word must not style it.
        self.pane.show(self._fh([1]), 'sub_temp', whole_word=True)
        self.root.update()
        start = self.pane.text.index(self.pane.text.tag_ranges('hl')[0])
        end = self.pane.text.index(self.pane.text.tag_ranges('hl')[1])
        self.assertEqual(self.pane.text.get(start, end), 'sub_temp')

    def test_match_case_highlight_ignores_differing_case(self):
        self.pane.show(self._fh([1]), 'SUB_TEMP', match_case=True)
        self.root.update()
        self.assertFalse(self.pane.text.tag_ranges('hl'),
                         'case-sensitive highlight must not match lowercase text')

    def test_jump_moves_to_the_next_hit_in_all_text(self):
        # In 'all text' the hits are lines 1 and 4; the jump buttons exist for
        # exactly this case, where hits are far apart.
        self.pane.show(self._fh([1, 4]), 'sub_temp')
        self.root.update()
        self.pane.mode_var.set('all')
        self.pane._rerender()
        self.pane._next_hit()
        self.assertEqual(self.pane.text.index('cur.first').split('.')[0], '1')
        self.pane._next_hit()
        self.assertEqual(self.pane.text.index('cur.first').split('.')[0], '4')

    def test_jump_highlights_the_whole_line_in_yellow(self):
        self.pane.show(self._fh([1, 4]), 'sub_temp')
        self.root.update()
        self.pane._next_hit()
        self.assertEqual(str(self.pane.text.tag_cget('cur', 'background')),
                         '#fff2a8')
        start, end = self.pane.text.tag_ranges('cur')
        self.assertEqual(self.pane.text.get(start, end),
                         self.lines[0])

    def test_jump_up_from_the_first_hit_stays_put(self):
        # Clamping, not wrapping: at the first hit the user should be told,
        # not silently sent to the last one.
        self.pane.show(self._fh([1, 4]), 'sub_temp')
        self.root.update()
        self.pane._next_hit()
        self.pane._prev_hit()
        self.assertEqual(self.pane.text.index('cur.first').split('.')[0], '1')
        self.assertIn('at the first', self.pane.info_var.get())

    def test_jump_targets_filter_matches_too(self):
        # The filter's matches are highlighted the same as the keyword's, so
        # the jump must step through them as well -- otherwise a green line
        # sits on screen that the buttons refuse to reach.
        self.pane.show(self._fh([1, 4]), 'sub_temp')
        self.root.update()
        self.pane.mode_var.set('all')
        self.pane.filter_var.set('3171')
        self.pane._rerender()
        # Rows 1, 3 (filter) and 4 (keyword) carry a highlight; row 2 does not.
        self.assertEqual(self.pane._hit_rows, [0, 2, 3])
        self.pane._next_hit()
        self.assertEqual(self.pane.text.index('cur.first').split('.')[0], '1')
        self.pane._next_hit()
        self.assertEqual(self.pane.text.index('cur.first').split('.')[0], '3')
        self.assertIn('highlight 2 of 3', self.pane.info_var.get())
        self.pane._next_hit()
        self.assertEqual(self.pane.text.index('cur.first').split('.')[0], '4')

    def test_jump_targets_only_the_intersection_of_keyword_and_filter(self):
        # When one line matches both, it is a single target: the two sources
        # must not produce duplicate stops on the same row.
        self.pane.show(self._fh([1, 4]), 'sub_temp')
        self.root.update()
        self.pane.filter_var.set('sub_temp')
        self.pane._rerender()
        self.assertEqual(self.pane._hit_rows, [0, 1])

    def test_jump_reports_when_nothing_is_highlighted(self):
        # No keyword match and no filter term: there is nothing on screen to
        # step to, and the pane must say so rather than silently doing nothing.
        self.pane.show(self._fh([1, 4]), 'NOTHING-HERE')
        self.root.update()
        self.pane._next_hit()
        self.assertIn('no highlighted lines', self.pane.info_var.get())
        self.assertFalse(self.pane.text.tag_ranges('cur'))

    def test_jump_reports_position(self):
        self.pane.show(self._fh([1, 4]), 'sub_temp')
        self.root.update()
        self.pane._next_hit()
        self.assertIn('highlight 1 of 2', self.pane.info_var.get())
        self.pane._next_hit()
        self.assertIn('highlight 2 of 2', self.pane.info_var.get())

    def test_selecting_another_file_clears_the_jump_highlight(self):
        self.pane.show(self._fh([1, 4]), 'sub_temp')
        self.root.update()
        self.pane._next_hit()
        self.assertTrue(self.pane.text.tag_ranges('cur'))
        self.pane.show(self._fh([4]), 'sub_temp')
        self.root.update()
        self.assertFalse(self.pane.text.tag_ranges('cur'))
        self.pane._next_hit()
        self.assertEqual(self.pane.text.index('cur.first').split('.')[0], '1')

    def test_error_row_does_not_crash(self):
        from log_analyzer.manifest import FileEntry
        from log_analyzer.search import FileHits
        e = FileEntry(id=0, vp='a.zip/x.log', disk='data/00/00000000', size=1, depth=0)
        self.pane.show(FileHits(e, [], 0, False, 'utf-8', False, error='boom'), 'k')
        self.root.update()
        self.assertIn('boom', self.pane.text.get('1.0', 'end-1c'))


if __name__ == '__main__':
    unittest.main()
