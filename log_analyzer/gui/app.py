import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .. import settings
from .. import __version__
from ..extractor import CancelToken, extract_archive, scan_archive
from ..flat import scan_flat, scan_tree
from ..manifest import (
    clear_workspace, index_upsert, index_touch, inspect_workspace,
    make_ref, read_manifest, scan_workspace,
)
from ..paths import archive_key, free_space, human_bytes, workspace_root
from ..scope import Scope, for_archive, for_flat, for_folder_tree
from ..search import SearchOptions, parse_terms, search
from ..sniff import sniff_file
from ..workers import start_job
from .detail_view import DetailPane
from .results_view import ResultTree


def _pick_target(parent=None):
    """Ask which kind of thing to open. Returns 'file', 'folder' or None.

    tkinter can filter a file dialog or pick a directory, but no native dialog
    does both, and the user asked for one Open entry. A tiny modal with two
    buttons is the smallest thing that keeps that promise -- cancelling it is
    indistinguishable from cancelling the dialog that would have followed.
    """
    win = tk.Toplevel(parent) if parent is not None else tk.Toplevel()
    win.title('Open')
    win.resizable(False, False)
    win.transient(parent)
    result = {'value': None}

    def choose(value):
        result['value'] = value
        win.destroy()

    body = ttk.Frame(win, padding=16)
    body.pack(fill='both', expand=True)
    ttk.Label(body, text='What would you like to open?').pack(pady=(0, 10))
    row = ttk.Frame(body)
    row.pack()
    ttk.Button(row, text='An archive or a single file…',
               command=lambda: choose('file')).pack(side='left', padx=4)
    ttk.Button(row, text='A folder…',
               command=lambda: choose('folder')).pack(side='left', padx=4)
    win.protocol('WM_DELETE_WINDOW', lambda: choose(None))
    win.bind('<Escape>', lambda e: choose(None))
    if parent is not None:
        win.grab_set()
    win.wait_window()
    return result['value']


def _elide(path, limit=56):
    """Shorten a path for the toolbar without hiding which volume it is on.

    The user must be able to see where multi-GB of cache is going; a label
    that pushes the rest of the toolbar off screen helps nobody.
    """
    if len(path) <= limit:
        return path
    head = os.path.splitdrive(path)[0] or path[0]
    keep = limit - len(head) - 2
    return head + os.sep + '…' + path[-keep:]


class App(ttk.Frame):
    def __init__(self, master, workspace=None):
        super().__init__(master)
        self.master = master
        self.ws_root, self.ws_origin = settings.resolve_workspace(workspace)
        # What the current search covers: entries, the source that reads them
        # and (for a cached archive) the key. None until something is opened.
        self.scope = None
        self.job = None
        self.results = []
        # The options of the search that produced the visible rows. Kept so a
        # row selection highlights with the same case/whole-word rules the
        # search used, even if the checkboxes have since been toggled.
        self.search_opts = None
        self._build()
        self._refresh_cache_list()
        self._on_close_hook()

    # ------------------------------------------------------------------ ui

    def _build(self):
        self.master.title('Cluster Log Analyzer %s' % __version__)
        self.master.minsize(1000, 680)
        self.pack(fill='both', expand=True)

        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill='x')
        # Held as attributes so _set_busy can disable them: reopening during an
        # extraction would rebind self.path while the worker closure reads it.
        self.open_btn = ttk.Button(bar, text='Open…', command=self.on_open)
        self.open_btn.pack(side='left')
        ttk.Label(bar, text='  Cached:').pack(side='left')
        self.cache_var = tk.StringVar()
        self.cache_box = ttk.Combobox(bar, textvariable=self.cache_var,
                                      state='readonly', width=34)
        self.cache_box.pack(side='left', padx=4)
        self.cache_box.bind('<<ComboboxSelected>>', self.on_pick_cached)
        self.change_ws_btn = ttk.Button(bar, text='Change…',
                                        command=self.on_change_workspace)
        self.change_ws_btn.pack(side='left')
        self.ws_label = ttk.Label(bar, text='')
        self.ws_label.pack(side='left', padx=8)
        ttk.Button(bar, text='Clear workspace…',
                   command=self.on_clear).pack(side='right')

        sep = ttk.Separator(self, orient='horizontal')
        sep.pack(fill='x')

        sb = ttk.Frame(self, padding=(8, 6))
        sb.pack(fill='x')
        ttk.Label(sb, text='Keyword:').pack(side='left')
        self.kw_var = tk.StringVar()
        self.kw_entry = ttk.Entry(sb, textvariable=self.kw_var, width=42)
        self.kw_entry.pack(side='left', padx=4)
        self.kw_entry.bind('<Return>', lambda e: self.on_search())
        ttk.Label(sb, text='(space/comma separates terms; any match counts)').pack(
            side='left', padx=(0, 4))

        self.case_var = tk.BooleanVar(value=False)
        self.word_var = tk.BooleanVar(value=False)
        self.binary_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(sb, text='Match case',
                        variable=self.case_var).pack(side='left', padx=3)
        ttk.Checkbutton(sb, text='Whole word',
                        variable=self.word_var).pack(side='left', padx=3)
        ttk.Checkbutton(sb, text='Hide binary',
                        variable=self.binary_var).pack(side='left', padx=3)

        self.search_btn = ttk.Button(sb, text='Search', command=self.on_search)
        self.search_btn.pack(side='left', padx=(12, 3))
        self.cancel_btn = ttk.Button(sb, text='Cancel', command=self.on_cancel,
                                     state='disabled')
        self.cancel_btn.pack(side='left', padx=3)

        status = ttk.Frame(self, padding=(8, 2))
        status.pack(fill='x')
        self.bar = ttk.Progressbar(status, mode='determinate', length=320)
        self.bar.pack(side='left')
        self.status_var = tk.StringVar(value='Ready.')
        ttk.Label(status, textvariable=self.status_var).pack(side='left', padx=10)

        panes = ttk.PanedWindow(self, orient='vertical')
        panes.pack(fill='both', expand=True, padx=8, pady=6)

        top = ttk.Frame(panes)
        self.results = ResultTree(top, on_select=self.on_row_select,
                                  on_export_txt=self.on_export_file_txt)
        self.results.pack(fill='both', expand=True)
        panes.add(top, weight=4)

        bottom = ttk.Frame(panes)
        vp_row = ttk.Frame(bottom)
        vp_row.pack(fill='x')
        ttk.Label(vp_row, text='Full path:').pack(side='left')
        self.vp_var = tk.StringVar()
        self.vp_entry = ttk.Entry(vp_row, textvariable=self.vp_var,
                                  state='readonly')
        self.vp_entry.pack(side='left', fill='x', expand=True, padx=6)
        self.vp_entry.bind('<Control-c>', lambda e: 'break')
        ttk.Button(vp_row, text='Copy',
                   command=self._copy_vp).pack(side='left')
        self.detail = DetailPane(bottom, loader=self._load_lines)
        self.detail.pack(fill='both', expand=True)
        panes.add(bottom, weight=3)

        self._build_menu()

    def _build_menu(self):
        m = tk.Menu(self.master)
        f = tk.Menu(m, tearoff=0)
        f.add_command(label='Open…', command=self.on_open)
        f.add_separator()
        f.add_command(label='Export results as CSV…', command=lambda: self.on_export('csv'))
        f.add_command(label='Export results as JSON…', command=lambda: self.on_export('json'))
        f.add_separator()
        f.add_command(label='Exit', command=self.master.destroy)
        m.add_cascade(label='File', menu=f)

        w = tk.Menu(m, tearoff=0)
        w.add_command(label='Show workspace contents…', command=self.on_list_cache)
        w.add_command(label='Change workspace…', command=self.on_change_workspace)
        w.add_command(label='Clear current archive…', command=lambda: self.on_clear(only_current=True))
        w.add_command(label='Clear all archives…', command=self.on_clear)
        m.add_cascade(label='Workspace', menu=w)

        h = tk.Menu(m, tearoff=0)
        h.add_command(label='Search options…', command=self.on_help_search)
        h.add_command(label='About', command=self.on_about)
        m.add_cascade(label='Help', menu=h)
        self.master.config(menu=m)

    def _on_close_hook(self):
        self.master.protocol('WM_DELETE_WINDOW', self.on_close)

    # --------------------------------------------------------------- state

    def _set_busy(self, busy):
        state = 'disabled' if busy else 'normal'
        self.search_btn.configure(state=state)
        self.cancel_btn.configure(state='normal' if busy else 'disabled')
        # Opening or relocating the workspace mid-job would rebind state that
        # the running worker closure reads at execution time.
        for btn in (self.open_btn, self.change_ws_btn):
            btn.configure(state=state)

    def _refresh_cache_list(self):
        info = scan_workspace(self.ws_root)
        names = ['%s  (%s)' % (a.get('display_name') or a['key'],
                               human_bytes(a.get('bytes', 0)))
                 for a in info['archives']]
        self._cache_keys = [a['key'] for a in info['archives']]
        self.cache_box['values'] = names
        origin = {'env': ' (LOG_ANALYZER_WORKSPACE)',
                  'settings': ' (saved)'}.get(self.ws_origin, '')
        self.ws_label.configure(text='Workspace: %s in %d archive(s) · %s%s' % (
            human_bytes(info['total_bytes']), len(names),
            _elide(self.ws_root), origin))

    def _set_scope(self, scope):
        """Adopt a new search scope, dropping what belonged to the old one."""
        self.scope = scope
        self.results.clear()
        self.detail.clear()
        self.vp_var.set('')
        self.search_opts = None
        flat = bool(scope and scope.flat)
        self.results.tree.heading('#0', text=(
            'File (path inside folder)' if flat
            else 'File (path inside archive)'))

    # -------------------------------------------------------------- actions

    def on_open(self):
        """One entry point for everything the tool can be pointed at.

        tkinter's native dialogs cannot show files and folders in the same
        window, so a small chooser picks which dialog to open. What the user
        picks -- not the dialog -- decides how it is loaded.
        """
        choice = _pick_target()
        if choice is None:
            return
        if choice == 'folder':
            path = filedialog.askdirectory(
                title='Select a folder of logs to search')
        else:
            path = filedialog.askopenfilename(
                title='Select a log archive or a single log file',
                filetypes=[('Archives', '*.zip *.tar *.gz *.tgz *.bz2 *.xz *.7z *.rar'),
                           ('All files', '*.*')])
        if not path:
            return
        self._load(path)

    def on_change_workspace(self):
        """Pick a different extraction destination and remember it."""
        if self.job is not None:
            # The button is disabled while a job runs; this covers the menu.
            messagebox.showinfo(
                'Cluster Log Analyzer',
                'A job is running. Cancel it before changing the workspace.')
            return
        start = self.ws_root if os.path.isdir(self.ws_root) \
            else os.path.dirname(self.ws_root)
        chosen = filedialog.askdirectory(
            title='Choose the extraction destination',
            initialdir=start or None, mustexist=True)
        if not chosen:
            return
        chosen = workspace_root(chosen)
        if os.path.normcase(chosen) == os.path.normcase(self.ws_root):
            return
        ok, why = settings.workspace_is_usable(chosen)
        if not ok:
            messagebox.showerror('Cluster Log Analyzer',
                                 'That folder cannot be used:\n%s' % why)
            return
        if settings.save_workspace(chosen):
            self.status_var.set('Workspace changed to %s' % chosen)
        else:
            # A preference that could not be persisted must still work now.
            self.status_var.set('Workspace changed to %s for this session; '
                                'the preference could not be saved.' % chosen)
        self._adopt_workspace(chosen, 'settings')

    def _adopt_workspace(self, root, origin):
        """Reset everything bound to the old workspace location.

        The keyword box, the checkboxes and the in-file filter are user input
        rather than workspace state, so they survive -- matching how the
        'discard this extraction' path already behaves.
        """
        self.ws_root = workspace_root(root)
        self.ws_origin = origin
        self._set_scope(None)
        self._structural = None
        self.cache_var.set('')
        self._cache_keys = []
        self.cache_box['values'] = []
        self.bar.stop()
        self.bar.configure(mode='determinate', value=0)
        self._refresh_cache_list()

    def on_pick_cached(self, _event=None):
        i = self.cache_box.current()
        if i < 0 or i >= len(self._cache_keys):
            return
        key = self._cache_keys[i]
        man = read_manifest(self.ws_root, key)
        if man is None:
            messagebox.showwarning('Cluster Log Analyzer',
                                   'That cache entry could not be read.')
            self._refresh_cache_list()
            return
        self._set_scope(for_archive(self.ws_root, key, man))
        index_touch(self.ws_root, key)
        self._announce_loaded()
        if man.status != 'complete':
            self._offer_resume()

    def _load(self, path):
        self.path = os.path.abspath(path)
        # A folder is always a folder scope: its loose files are read where they
        # are, and any archive inside it is extracted and folded into the same
        # scope. A file that is not an archive is searched in place too --
        # copying it into the workspace first would cost the user the disk
        # space this tool exists to find things inside of.
        if os.path.isdir(self.path):
            self._load_flat(self.path)
            return
        if sniff_file(self.path) is None:
            self._load_flat(self.path)
            return
        self.key = archive_key(self.path)
        state, man, partial, done = inspect_workspace(self.ws_root, self.key)

        if state == 'complete' and man is not None:
            self._set_scope(for_archive(self.ws_root, self.key, man))
            self._announce_loaded()
            return
        if state == 'partial':
            self._offer_resume(structural=None)
            return

        # Preflight: walk the structure so the user sees the real cost before
        # committing multi-GB of disk.
        self.status_var.set('Scanning archive structure…')
        self._set_busy(True)
        self.bar.configure(mode='indeterminate')
        self.bar.start(12)
        app = self

        def work(job):
            try:
                s = scan_archive(app.path, job.cancel)
                job.emit('scan_done', s)
            except Exception as e:
                job.emit('error', str(e))

        self._run(work, self._on_scan_event)

    def _on_scan_event(self, kind, payload):
        if kind == 'scan_done':
            self.bar.stop()
            self.bar.configure(mode='determinate', value=0)
            self._set_busy(False)
            self._structural = payload
            ok = self._confirm_extract(payload)
            if ok:
                self._start_extract(payload)
            else:
                self.status_var.set('Cancelled before extraction.')
        elif kind == 'error':
            self.bar.stop()
            self.bar.configure(mode='determinate', value=0)
            self._set_busy(False)
            messagebox.showerror('Cluster Log Analyzer', payload)

    def _load_flat(self, root):
        """Enumerate a folder or single file, then adopt it as the scope.

        A folder takes the tree scan, which also extracts the archives found
        inside it; a single loose file is read strictly in place. Both land in
        the same event handler, tagged with which one ran.
        """
        is_dir = os.path.isdir(root)
        self.status_var.set('Scanning folder…' if is_dir else 'Opening file…')
        self._set_busy(True)
        self.bar.configure(mode='indeterminate')
        self.bar.start(12)
        ws_root = self.ws_root

        def work(job):
            try:
                if is_dir:
                    man, keys = scan_tree(
                        root, ws_root, cancel=job.cancel,
                        progress=lambda ph, n, t, note: job.emit(
                            'log', 'Scanning %d/%d — %s' % (n, t, note)))
                    job.emit('flat_done', ('tree', man, keys))
                else:
                    man = scan_flat(
                        root, cancel=job.cancel, ws_root=ws_root,
                        progress=lambda ph, n, t, note: job.emit(
                            'log', 'Scanning %d — %s' % (n, note)))
                    job.emit('flat_done', ('single', man, ()))
            except Cancelled:
                # Must be its own event: falling into the generic handler
                # below would pop an empty-text error dialog on every cancel.
                job.emit('flat_cancelled', None)
            except Exception as e:
                job.emit('error', str(e))

        self._run(work, self._on_flat_event)

    def _on_flat_event(self, kind, payload):
        if kind == 'log':
            self.status_var.set(str(payload)[:110])
        elif kind == 'flat_done':
            mode, man, keys = payload
            self.bar.stop()
            self.bar.configure(mode='determinate', value=0)
            self._set_busy(False)
            # Both scans record the abspath root in archive_name; using it
            # rather than self.path keeps a folder picked after a previous
            # archive from being labelled with the archive's path.
            if mode == 'tree':
                self._set_scope(for_folder_tree(man.archive_name, man,
                                                self.ws_root, keys))
                self._refresh_cache_list()
            else:
                self._set_scope(for_flat(man.archive_name, man))
            self._announce_loaded()
        elif kind == 'flat_cancelled':
            self.bar.stop()
            self.bar.configure(mode='determinate', value=0)
            self._set_busy(False)
            self.status_var.set('Folder scan cancelled.')

    def _confirm_extract(self, s):
        free = free_space(self.ws_root)
        msg = ('This archive expands to about %s across %d files '
               '(%d nested archives).\n\nWorkspace:\n%s\nFree space on that '
               'volume: %s\n\nExtract now?' % (
                   human_bytes(s.total_bytes), s.files, s.archives,
                   self.ws_root, human_bytes(free) if free >= 0 else 'unknown'))
        if free >= 0 and s.total_bytes > free * 0.9:
            msg += '\n\nWARNING: this may not fit on the target volume.'
        return messagebox.askyesno('Confirm extraction', msg)

    def _offer_resume(self, structural=None):
        state, man, partial, done = inspect_workspace(self.ws_root, self.key)
        if state != 'partial':
            self._set_scope(None)
            return
        n, total = len(done), len(partial)
        if not messagebox.askyesno(
                'Interrupted extraction',
                'A previous extraction of this archive was interrupted.\n\n'
                '%d part(s) are complete, %d file(s) are usable.\n\n'
                'Yes: resume where it stopped.\nNo: discard and start over.' % (n, total)):
            clear_workspace(self.ws_root, self.key)
            self._refresh_cache_list()
            self._set_scope(None)
            self.status_var.set('Previous extraction discarded.')
            return
        self._start_extract(structural, resume=True)

    def _start_extract(self, structural=None, resume=False):
        self._set_busy(True)
        self.bar.configure(mode='determinate', value=0,
                           maximum=max(getattr(structural, 'top_level_entries', 0), 1))
        self.status_var.set('Extracting…')
        app = self
        # Captured now, not re-read when the job finishes: the event handler
        # must label the result with the key the job actually used.
        key = self.key

        def work(job):
            man = extract_archive(
                app.path, app.ws_root, key, cancel=job.cancel,
                resume=resume, structural=structural,
                log=lambda m: job.emit('log', m),
                progress=lambda ph, d, t, note: job.emit('progress', (ph, d, t, note)))
            job.emit('extracted', (key, man))

        self._run(work, self._on_extract_event)

    def _on_extract_event(self, kind, payload):
        if kind == 'progress':
            phase, done, total, note = payload
            self.bar.configure(maximum=max(total, 1), value=done)
            self.status_var.set('Extracting %d/%d — %s %s' % (
                done, total, note, ''))
        elif kind == 'log':
            self.status_var.set(str(payload)[:110])
        elif kind == 'extracted':
            key, man = payload
            self._set_scope(for_archive(self.ws_root, key, man))
            self.bar.configure(value=self.bar['maximum'])
            self._set_busy(False)
            index_upsert(self.ws_root, make_ref(key, man))
            self._refresh_cache_list()
            self._announce_loaded()
            if man.status != 'complete':
                self.status_var.set(
                    'Extraction stopped early: %d of %s parts complete. '
                    'Press Search to use what is there, or reopen to resume.' % (
                        payload.stats.get('completed_entries', 0),
                        payload.stats.get('top_level_entries', '?')))

    def _announce_loaded(self):
        man = self.scope.manifest
        n = len(man.entries)
        extra = '' if man.status == 'complete' else ' (partial)'
        if self.scope.flat:
            extra += ' (searched in place)'
        # Say so when archives inside the folder were extracted: that is the
        # reason the workspace grew, and the reason a later run is faster.
        if self.scope.local_keys:
            extra += ' — %d archive(s) in the folder extracted' % len(
                self.scope.local_keys)
        if man.warnings:
            extra += ' — %d warning(s)' % len(man.warnings)
        self.status_var.set('Ready: %s — %d files%s. Enter a keyword.' % (
            man.archive_name[:60], n, extra))

    def on_search(self):
        if self.scope is None:
            messagebox.showinfo('Cluster Log Analyzer',
                                'Open an archive or a folder first.')
            return
        keyword = self.kw_var.get().strip()
        # The box must contain at least one term once separators are removed:
        # a value like ',,,' is non-empty but would compile to a pattern that
        # matches every line.
        if not parse_terms(keyword):
            messagebox.showinfo('Cluster Log Analyzer',
                                'Enter a keyword to search for.')
            return
        if self.scope.root and not os.path.exists(self.scope.root):
            messagebox.showinfo('Cluster Log Analyzer',
                                'That folder is no longer there:\n%s' % self.scope.root)
            return
        if self.scope.manifest.status != 'complete':
            ok = messagebox.askyesno(
                'Partial extraction',
                'This extraction is incomplete (%d of %s parts).\n\n'
                'Searching now will only cover the files already extracted. '
                'Continue?' % (self.scope.manifest.stats.get('completed_entries', 0),
                               self.scope.manifest.stats.get('top_level_entries', '?')))
            if not ok:
                return

        self.results.clear()
        self.detail.clear()
        self.vp_var.set('')
        self._set_busy(True)
        self.bar.configure(mode='determinate', value=0,
                           maximum=max(len(self.scope.entries), 1))
        self.status_var.set('Searching…')

        opts = SearchOptions(
            keyword=keyword,
            match_case=self.case_var.get(),
            whole_word=self.word_var.get(),
            include_binary=not self.binary_var.get())
        self.search_opts = opts
        entries = self.scope.entries
        source = self.scope.source

        def work(job):
            s = search(entries, source, opts, cancel=job.cancel,
                       sink=lambda b: job.emit('files', b),
                       progress=lambda ph, d, t, note: job.emit(
                           'progress', (ph, d, t, note)))
            job.emit('search_done', s)

        self._run(work, self._on_search_event)

    def _on_search_event(self, kind, payload):
        if kind == 'files':
            self.results.append_batch(payload)
        elif kind == 'progress':
            _ph, done, total, note = payload
            self.bar.configure(maximum=max(total, 1), value=done)
            self.status_var.set('Searching %d/%d — %d files matched' % (
                done, total, self.results.file_count()))
        elif kind == 'search_done':
            self.bar.configure(value=self.bar['maximum'])
            self._set_busy(False)
            self.status_var.set(payload.describe())
            self.export_summary = payload

    def on_cancel(self):
        if self.job is not None:
            self.job.cancel.cancel()
            self.status_var.set('Cancelling… (kept results so far)')

    def on_row_select(self, fh):
        self.vp_var.set(fh.entry.vp)
        # The pane reloads the file from disk rather than reusing fh.hits: the
        # 'all text' and in-file filter modes both need the whole file, and
        # hits only carry the first max_hits_per_file lines. The options come
        # from the search that produced this row, not the live checkboxes, so
        # toggling 'Match case' afterwards cannot restyle an existing result.
        opts = self.search_opts
        self.detail.show(fh, self.kw_var.get().strip(),
                         match_case=opts.match_case if opts else False,
                         whole_word=opts.whole_word if opts else False)

    def _load_lines(self, entry):
        """Read one leaf as display lines. Runs on the main thread.

        Reading is milliseconds for the largest file in this corpus (524 KB),
        so a worker job would add more machinery than it saves.
        """
        from ..detail import read_lines
        from ..textio import detect_codec, read_sample
        source = self.scope.source
        with source.open(entry) as fh:
            codec = detect_codec(read_sample(fh))
        return read_lines(source, entry, codec)

    def _copy_vp(self):
        v = self.vp_var.get()
        if v:
            self.clipboard_clear()
            self.clipboard_append(v)

    def on_export_file_txt(self, fh):
        """Write one member out as a plain .txt, in full.

        The whole decoded file, not just the hit lines: the point of this is
        to hand a single log to someone else (or to grep it with other tools),
        and a file of isolated matching lines is not a log any more. Text is
        written as UTF-8 regardless of what the member was encoded in, so the
        result opens cleanly everywhere -- that also means a GBK log is
        transcoded rather than copied byte-for-byte.
        """
        from ..detail import read_lines
        from ..naming import export_stem
        from ..textio import detect_codec, read_sample

        if fh.error:
            messagebox.showerror('Cluster Log Analyzer',
                                 'This file could not be read:\n%s' % fh.error)
            return

        path = filedialog.asksaveasfilename(
            defaultextension='.txt',
            initialfile=export_stem(fh.entry.vp) + '.txt',
            filetypes=[('Text', '*.txt'), ('All files', '*.*')])
        if not path:
            return

        source = self.scope.source
        try:
            with source.open(fh.entry) as f:
                codec = detect_codec(read_sample(f))
            lines, truncated = read_lines(source, fh.entry, codec)
            with open(path, 'w', encoding='utf-8', newline='\n') as out:
                out.write('\n'.join(lines))
                if lines:
                    out.write('\n')
        except OSError as e:
            messagebox.showerror('Cluster Log Analyzer',
                                 'Could not write file:\n%s' % e)
            return

        note = 'Exported %d lines to %s' % (len(lines), path)
        if truncated:
            # read_lines caps at MAX_DETAIL_BYTES/LINES; saying so beats
            # letting the user believe they hold a complete log.
            note += ' (file was too large to export in full)'
        self.status_var.set(note)

    def on_export(self, fmt):
        rows = []
        for iid, fh in self.results._files.items():
            for h in fh.hits:
                rows.append((fh.entry.vp, h.line_no, h.line))
        if not rows:
            messagebox.showinfo('Cluster Log Analyzer', 'No results to export.')
            return
        ext = '.csv' if fmt == 'csv' else '.json'
        path = filedialog.asksaveasfilename(defaultextension=ext,
                                            filetypes=[(fmt.upper(), '*' + ext)])
        if not path:
            return
        try:
            if fmt == 'csv':
                import csv
                with open(path, 'w', newline='', encoding='utf-8-sig') as f:
                    w = csv.writer(f)
                    w.writerow(['virtual_path', 'line_no', 'line'])
                    w.writerows(rows)
            else:
                import json
                data = [{'virtual_path': r[0], 'line_no': r[1], 'line': r[2]}
                        for r in rows]
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
        except OSError as e:
            messagebox.showerror('Cluster Log Analyzer', 'Could not write file:\n%s' % e)
            return
        self.status_var.set('Exported %d rows to %s' % (len(rows), path))

    def on_list_cache(self):
        info = scan_workspace(self.ws_root)
        if not info['archives']:
            messagebox.showinfo('Workspace', 'The workspace is empty.\n\n%s' %
                                self.ws_root)
            return
        lines = ['%s\n    %s  %s files  %s' % (
            a.get('display_name') or a['key'], human_bytes(a.get('bytes', 0)),
            a.get('files', '?'), a.get('status', '?')) for a in info['archives']]
        messagebox.showinfo('Workspace', '%s\n\nTotal: %s\n\n%s' % (
            self.ws_root, human_bytes(info['total_bytes']), '\n'.join(lines)))

    def on_clear(self, only_current=False):
        info = scan_workspace(self.ws_root)
        if only_current:
            if self.scope is None:
                messagebox.showinfo('Cluster Log Analyzer', 'No archive loaded.')
                return
            if self.scope.local_keys:
                # A folder scope extracted the archives it found; those are the
                # only thing it wrote, so clearing them returns exactly the
                # cache this scope consumed. The loose files are untouched.
                if not messagebox.askyesno(
                        'Clear extracted archives',
                        'Delete the extracted copies of %d archive(s) found in '
                        'this folder?' % len(self.scope.local_keys)):
                    return
                try:
                    for key in self.scope.local_keys:
                        clear_workspace(self.ws_root, key)
                except OSError as e:
                    messagebox.showerror('Cluster Log Analyzer',
                                         'Could not clear:\n%s' % e)
                    return
                self._set_scope(None)
                self._refresh_cache_list()
                self.status_var.set('Cleared the archives extracted from that folder.')
                return
            elif self.scope.flat:
                messagebox.showinfo(
                    'Cluster Log Analyzer',
                    'This folder is searched in place, so there is nothing cached to clear.')
                return
            if not messagebox.askyesno('Clear archive',
                                       'Delete the extracted data for the current archive?'):
                return
            try:
                clear_workspace(self.ws_root, self.scope.key)
            except OSError as e:
                messagebox.showerror('Cluster Log Analyzer', 'Could not clear:\n%s' % e)
                return
            self._set_scope(None)
            self.status_var.set('Cleared the current archive.')
        else:
            if not info['archives']:
                messagebox.showinfo('Cluster Log Analyzer', 'The workspace is already empty.')
                return
            if not messagebox.askyesno(
                    'Clear workspace',
                    'Delete all %d cached archive(s), freeing %s?\n\n%s' % (
                        len(info['archives']), human_bytes(info['total_bytes']),
                        self.ws_root)):
                return
            try:
                clear_workspace(self.ws_root)
            except OSError as e:
                messagebox.showerror('Cluster Log Analyzer', 'Could not clear:\n%s' % e)
                return
            self._set_scope(None)
            self.status_var.set('Workspace cleared.')
        self._refresh_cache_list()

    def on_help_search(self):
        messagebox.showinfo(
            'Search options',
            'The keyword is matched literally — it is not a regular '
            'expression, so . and * have no special meaning.\n\n'
            'Several keywords\n    Separate them with a space, comma or '
            'semicolon. A line counts as a match when ANY of them appears '
            '(OR), the same rule the in-file filter uses.\n\n'
            'Match case\n    Off (default): case-insensitive.\n'
            '    On: exact case required.\n\n'
            'Whole word\n    The match must not be surrounded by letters, '
            'digits or underscore. This composes with Match case.\n\n'
            'Note: with an ASCII keyword, case-insensitive matching follows '
            'ASCII rules only, so "cafe" will not match "CAFÉ". This keeps '
            'GBK-encoded logs readable.\n\n'
            'What gets searched\n'
            '    Open an archive: every file inside it, at any depth.\n'
            '    Open a folder: every file in it and in every subfolder, and '
            'the contents of any archive found along the way.')

    def on_about(self):
        scope = ''
        if self.scope is not None:
            how = 'in place' if self.scope.flat else 'from the cache'
            if self.scope.local_keys:
                how += ' (%d archive(s) inside it extracted)' % len(
                    self.scope.local_keys)
            scope = '\n\nSearching %s:\n%s' % (how, self.scope.root)
        messagebox.showinfo('About', 'Cluster Log Analyzer %s\n\nRecursive nested-archive '
                            'extraction and keyword search.\n\nWorkspace:\n%s%s' %
                            (__version__, self.ws_root, scope))

    # ------------------------------------------------------------- plumbing

    def _run(self, work, on_event):
        """Start a job whose events are dispatched on the main thread."""
        def dispatch(kind, payload):
            if kind == '__end__':
                self.job = None
                return
            if kind == 'error':
                self._set_busy(False)
                self.bar.stop()
                messagebox.showerror('Cluster Log Analyzer', payload)
                return
            on_event(kind, payload)

        self.job = start_job(work, dispatch, self.master)

    def on_close(self):
        if self.job is not None:
            self.job.cancel.cancel()
        self.master.destroy()


def main(workspace=None, archive=None):
    root = tk.Tk()
    try:
        root.call('tk', 'scaling', 1.25)
    except tk.TclError:
        pass
    app = App(root, workspace=workspace)
    if archive:
        root.after(200, lambda: app._load(archive))
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
