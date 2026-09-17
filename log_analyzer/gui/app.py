import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .. import __version__
from ..extractor import CancelToken, extract_archive, scan_archive
from ..manifest import (
    clear_workspace, index_upsert, index_touch, inspect_workspace,
    make_ref, read_manifest, scan_workspace,
)
from ..paths import archive_key, free_space, human_bytes, workspace_root
from ..search import SearchOptions, search
from ..source import DiskSource
from ..workers import start_job
from .detail_view import DetailPane
from .results_view import ResultTree


class App(ttk.Frame):
    def __init__(self, master, workspace=None):
        super().__init__(master)
        self.master = master
        self.ws_root = workspace_root(workspace)
        self.key = None
        self.manifest = None
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
        ttk.Button(bar, text='Open archive…',
                   command=self.on_open).pack(side='left')
        ttk.Label(bar, text='  Cached:').pack(side='left')
        self.cache_var = tk.StringVar()
        self.cache_box = ttk.Combobox(bar, textvariable=self.cache_var,
                                      state='readonly', width=52)
        self.cache_box.pack(side='left', padx=4)
        self.cache_box.bind('<<ComboboxSelected>>', self.on_pick_cached)
        self.ws_label = ttk.Label(bar, text='')
        self.ws_label.pack(side='left', padx=10)
        ttk.Button(bar, text='Clear workspace…',
                   command=self.on_clear).pack(side='right')

        sep = ttk.Separator(self, orient='horizontal')
        sep.pack(fill='x')

        sb = ttk.Frame(self, padding=(8, 6))
        sb.pack(fill='x')
        ttk.Label(sb, text='Keyword:').pack(side='left')
        self.kw_var = tk.StringVar()
        self.kw_entry = ttk.Entry(sb, textvariable=self.kw_var, width=34)
        self.kw_entry.pack(side='left', padx=4)
        self.kw_entry.bind('<Return>', lambda e: self.on_search())

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
        self.results = ResultTree(top, on_select=self.on_row_select)
        self.results.pack(fill='both', expand=True)
        panes.add(top, weight=4)

        bottom = ttk.Frame(panes)
        vp_row = ttk.Frame(bottom)
        vp_row.pack(fill='x')
        ttk.Label(vp_row, text='Full path in archive:').pack(side='left')
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
        f.add_command(label='Open archive…', command=self.on_open)
        f.add_command(label='Export results as CSV…', command=lambda: self.on_export('csv'))
        f.add_command(label='Export results as JSON…', command=lambda: self.on_export('json'))
        f.add_separator()
        f.add_command(label='Exit', command=self.master.destroy)
        m.add_cascade(label='File', menu=f)

        w = tk.Menu(m, tearoff=0)
        w.add_command(label='Show workspace contents…', command=self.on_list_cache)
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

    def _refresh_cache_list(self):
        info = scan_workspace(self.ws_root)
        names = ['%s  (%s)' % (a.get('display_name') or a['key'],
                               human_bytes(a.get('bytes', 0)))
                 for a in info['archives']]
        self._cache_keys = [a['key'] for a in info['archives']]
        self.cache_box['values'] = names
        self.ws_label.configure(text='Workspace: %s in %d archive(s)' % (
            human_bytes(info['total_bytes']), len(names)))

    # -------------------------------------------------------------- actions

    def on_open(self):
        path = filedialog.askopenfilename(
            title='Select a log archive',
            filetypes=[('Archives', '*.zip *.tar *.gz *.tgz *.bz2 *.xz *.7z *.rar'),
                       ('All files', '*.*')])
        if not path:
            return
        self._load(path)

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
        self.key = key
        self.manifest = man
        index_touch(self.ws_root, key)
        self._announce_loaded()
        if man.status != 'complete':
            self._offer_resume()

    def _load(self, path):
        self.path = os.path.abspath(path)
        self.key = archive_key(self.path)
        state, man, partial, done = inspect_workspace(self.ws_root, self.key)

        if state == 'complete' and man is not None:
            self.manifest = man
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
            self.key = None
            self.manifest = None
            return
        n, total = len(done), len(partial)
        if not messagebox.askyesno(
                'Interrupted extraction',
                'A previous extraction of this archive was interrupted.\n\n'
                '%d part(s) are complete, %d file(s) are usable.\n\n'
                'Yes: resume where it stopped.\nNo: discard and start over.' % (n, total)):
            clear_workspace(self.ws_root, self.key)
            self._refresh_cache_list()
            self.key = None
            self.manifest = None
            self.status_var.set('Previous extraction discarded.')
            return
        self._start_extract(structural, resume=True)

    def _start_extract(self, structural=None, resume=False):
        self._set_busy(True)
        self.bar.configure(mode='determinate', value=0,
                           maximum=max(getattr(structural, 'top_level_entries', 0), 1))
        self.status_var.set('Extracting…')
        app = self

        def work(job):
            man = extract_archive(
                app.path, app.ws_root, app.key, cancel=job.cancel,
                resume=resume, structural=structural,
                log=lambda m: job.emit('log', m),
                progress=lambda ph, d, t, note: job.emit('progress', (ph, d, t, note)))
            job.emit('extracted', man)

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
            self.manifest = payload
            self.bar.configure(value=self.bar['maximum'])
            self._set_busy(False)
            index_upsert(self.ws_root, make_ref(self.key, payload))
            self._refresh_cache_list()
            self._announce_loaded()
            if payload.status != 'complete':
                self.status_var.set(
                    'Extraction stopped early: %d of %s parts complete. '
                    'Press Search to use what is there, or reopen to resume.' % (
                        payload.stats.get('completed_entries', 0),
                        payload.stats.get('top_level_entries', '?')))

    def _announce_loaded(self):
        n = len(self.manifest.entries)
        extra = '' if self.manifest.status == 'complete' else ' (partial)'
        self.status_var.set('Ready: %s — %d files%s. Enter a keyword.' % (
            self.manifest.archive_name[:60], n, extra))

    def on_search(self):
        if self.manifest is None:
            messagebox.showinfo('Cluster Log Analyzer', 'Open an archive first.')
            return
        keyword = self.kw_var.get().strip()
        if not keyword:
            messagebox.showinfo('Cluster Log Analyzer', 'Enter a keyword to search for.')
            return
        if self.manifest.status != 'complete':
            ok = messagebox.askyesno(
                'Partial extraction',
                'This extraction is incomplete (%d of %s parts).\n\n'
                'Searching now will only cover the files already extracted. '
                'Continue?' % (self.manifest.stats.get('completed_entries', 0),
                               self.manifest.stats.get('top_level_entries', '?')))
            if not ok:
                return

        self.results.clear()
        self.detail.clear()
        self.vp_var.set('')
        self._set_busy(True)
        self.bar.configure(mode='determinate', value=0,
                           maximum=max(len(self.manifest.entries), 1))
        self.status_var.set('Searching…')

        opts = SearchOptions(
            keyword=keyword,
            match_case=self.case_var.get(),
            whole_word=self.word_var.get(),
            include_binary=not self.binary_var.get())
        self.search_opts = opts
        entries = self.manifest.entries
        source = DiskSource(self.ws_root, self.key)

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
        source = DiskSource(self.ws_root, self.key)
        with source.open(entry) as fh:
            codec = detect_codec(read_sample(fh))
        return read_lines(source, entry, codec)

    def _copy_vp(self):
        v = self.vp_var.get()
        if v:
            self.clipboard_clear()
            self.clipboard_append(v)

    def on_export(self, fmt):
        if not getattr(self, '_export_iids', None):
            pass
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
            if not self.key:
                messagebox.showinfo('Cluster Log Analyzer', 'No archive loaded.')
                return
            if not messagebox.askyesno('Clear archive',
                                       'Delete the extracted data for the current archive?'):
                return
            try:
                clear_workspace(self.ws_root, self.key)
            except OSError as e:
                messagebox.showerror('Cluster Log Analyzer', 'Could not clear:\n%s' % e)
                return
            self.key = None
            self.manifest = None
            self.results.clear()
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
            self.key = None
            self.manifest = None
            self.results.clear()
            self.status_var.set('Workspace cleared.')
        self._refresh_cache_list()

    def on_help_search(self):
        messagebox.showinfo(
            'Search options',
            'The keyword is matched literally — it is not a regular '
            'expression, so . and * have no special meaning.\n\n'
            'Match case\n    Off (default): case-insensitive.\n'
            '    On: exact case required.\n\n'
            'Whole word\n    The match must not be surrounded by letters, '
            'digits or underscore. This composes with Match case.\n\n'
            'Note: with an ASCII keyword, case-insensitive matching follows '
            'ASCII rules only, so "cafe" will not match "CAFÉ". This keeps '
            'GBK-encoded logs readable.')

    def on_about(self):
        messagebox.showinfo('About', 'Cluster Log Analyzer %s\n\nRecursive nested-archive '
                            'extraction and keyword search.\n\nWorkspace:\n%s' %
                            (__version__, self.ws_root))

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
