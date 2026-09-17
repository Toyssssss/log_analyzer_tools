import os
import tkinter as tk
from tkinter import ttk

from ..naming import shorten_vp


class ResultTree(ttk.Frame):
    """File rows at the top level, hit lines inserted lazily on expand.

    Eagerly inserting every hit line would be tens of thousands of rows in the
    worst case; instead only matching files are inserted up front (typically
tens to low hundreds), and a file's lines are materialised the first time the
    user expands it. Collapsing hides them again, which is exactly the
    expand/collapse behaviour the results are meant to have.
    """

    def __init__(self, master, on_select=None):
        super().__init__(master)
        self.on_select = on_select or (lambda fh: None)
        self._files = {}        # iid -> FileHits
        self._lines = {}        # iid -> set of child iids already built

        cols = ('path', 'hits')
        self.tree = ttk.Treeview(self, columns=cols, show='tree headings',
                                 selectmode='browse')
        self.tree.heading('#0', text='File (path inside archive)')
        self.tree.heading('path', text='')
        self.tree.heading('hits', text='Hits')
        self.tree.column('#0', width=760, minwidth=200, stretch=True)
        self.tree.column('path', width=0, minwidth=0, stretch=False)
        self.tree.column('hits', width=110, minwidth=80, stretch=False,
                         anchor='e')
        self.tree.bind('<<TreeviewOpen>>', self._on_open)
        self.tree.bind('<<TreeviewSelect>>', self._on_select)

        vsb = ttk.Scrollbar(self, orient='vertical', command=self.tree.yview)
        hsb = ttk.Scrollbar(self, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.popup = tk.Menu(self, tearoff=0)
        self.popup.add_command(label='Copy virtual path',
                               command=self._copy_vp)
        self.popup.add_separator()
        self.popup.add_command(label='Expand all shown files',
                               command=self.expand_all)
        self.popup.add_command(label='Collapse all', command=self.collapse_all)
        if sys_windows():
            self.tree.bind('<Button-3>', self._show_popup)
        else:
            self.tree.bind('<Button-3>', self._show_popup)
            self.tree.bind('<Button-2>', self._show_popup)

    # ---------------------------------------------------------------- events

    def _show_popup(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
        try:
            self.popup.tk_popup(event.x_root, event.y_root)
        finally:
            self.popup.grab_release()

    def _copy_vp(self):
        fh = self.selected_file()
        if fh is not None:
            self.clipboard_clear()
            self.clipboard_append(fh.entry.vp)

    def _on_open(self, _event):
        iid = self.tree.focus()
        if iid in self._files:
            self._materialise(iid)

    def _on_select(self, _event):
        fh = self.selected_file()
        if fh is not None:
            self.on_select(fh)

    def _materialise(self, iid):
        """Insert a file's hit lines once. Collapsing simply hides them."""
        if iid in self._lines:
            return
        fh = self._files[iid]
        self._lines[iid] = True
        for h in fh.hits:
            self.tree.insert(iid, 'end', text='%6d' % h.line_no,
                             values=('', h.line))

    # ------------------------------------------------------------------- api

    def clear(self):
        self.tree.delete(*self.tree.get_children(''))
        self._files.clear()
        self._lines.clear()

    def append_batch(self, batch):
        for fh in batch:
            label = shorten_vp(fh.entry.vp)
            if fh.error:
                hits_text = 'error'
            elif fh.truncated:
                hits_text = '%d (first %d)' % (fh.total_hits, len(fh.hits))
            else:
                hits_text = str(fh.total_hits)
            if fh.binary:
                label = '◈ ' + label
            iid = self.tree.insert('', 'end', text=label,
                                   values=(fh.entry.vp, hits_text))
            self._files[iid] = fh

    def selected_file(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return self._files.get(sel[0])

    def expand_all(self):
        for iid in list(self._files):
            self._materialise(iid)
            self.tree.item(iid, open=True)

    def collapse_all(self):
        for iid in list(self._files):
            self.tree.item(iid, open=False)

    def file_count(self):
        return len(self._files)


def sys_windows():
    return os.name == 'nt'
