import bisect
import os
import tkinter as tk
from tkinter import ttk

from ..detail import (
    keyword_pattern, match_ranges, parse_terms, select_lines,
)

MONO = ('Consolas' if os.name == 'nt' else 'Monospace', 9)
HL_BG = '#ccffcc'      # light green, as requested
HL_FG = '#0a3d0a'
CUR_BG = '#fff2a8'     # light yellow, for the line the jump buttons land on


class DetailPane(ttk.Frame):
    """The lower pane: full file text, with in-file filtering and highlighting.

    Two display modes share one render path. 'All text' shows the whole file;
    'Matched only' restricts the base set to the lines that matched the
    original search. The filter box then applies on top of whichever base set
    is active, so the two controls compose instead of fighting.

    Every term -- process/thread number or keyword alike -- is matched as
    literal text and the terms are OR'd. Measured on the real corpus only
    19.1% of lines are hilog-shaped, so treating numbers as a pid column would
    silently do nothing on the other 81% (kmsg, el2, JSON).
    """

    def __init__(self, master, loader):
        super().__init__(master)
        # loader(entry) -> (lines, truncated); supplied by App so this widget
        # never needs to know about the workspace or the archive layout.
        self.loader = loader
        self._entry = None
        self._lines = []
        self._truncated = False
        self._keyword = ''
        self._match_case = False
        self._whole_word = False
        self._hit_lines = []       # 0-based indices the search reported
        self._total_hits = 0       # true count; _hit_lines may be capped
        self._hit_rows = []        # displayed rows carrying a green highlight
        self._cur_row = None       # displayed row highlighted by a jump
        self._jump_note = ''       # transient jump feedback for the info line
        self._info_state = (0, 0, False, [], 0)
        self._load_token = 0

        self._build()

    # ----------------------------------------------------------------- setup

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill='x')

        self.mode_var = tk.StringVar(value='hits')
        ttk.Radiobutton(top, text='Matched lines only', value='hits',
                        variable=self.mode_var,
                        command=self._rerender).pack(side='left')
        ttk.Radiobutton(top, text='All file text', value='all',
                        variable=self.mode_var,
                        command=self._rerender).pack(side='left', padx=(8, 0))

        ttk.Separator(top, orient='vertical').pack(side='left', fill='y',
                                                   padx=10)
        ttk.Label(top, text='Filter in file:').pack(side='left')
        self.filter_var = tk.StringVar()
        self.filter_entry = ttk.Entry(top, textvariable=self.filter_var, width=30)
        self.filter_entry.pack(side='left', padx=4)
        self.filter_entry.bind('<Return>', lambda e: self._rerender())
        self.case_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text='Match case',
                        variable=self.case_var).pack(side='left', padx=3)
        ttk.Button(top, text='Apply',
                   command=self._rerender).pack(side='left', padx=(6, 2))
        ttk.Button(top, text='Clear',
                   command=self._clear_filter).pack(side='left')

        ttk.Separator(top, orient='vertical').pack(side='left', fill='y',
                                                   padx=10)
        # Jump between highlighted lines -- keyword hits and filter matches
        # alike, since both are what the user is scanning for. Most useful in
        # 'All file text', where the hits can be far apart.
        ttk.Button(top, text='↑', width=3, command=self._prev_hit,
                   ).pack(side='left', padx=(0, 2))
        ttk.Button(top, text='↓', width=3, command=self._next_hit,
                   ).pack(side='left')

        body = ttk.Frame(self)
        body.pack(fill='both', expand=True)

        self.text = tk.Text(body, wrap='none', font=MONO,
                            background='white', undo=False)
        vsb = ttk.Scrollbar(body, orient='vertical', command=self.text.yview)
        self._vsb = vsb
        hsb = ttk.Scrollbar(body, orient='horizontal', command=self.text.xview)
        self.text.configure(yscrollcommand=self._on_text_scroll,
                            xscrollcommand=hsb.set)

        # A second Text used purely as a static line-number gutter; keeping the
        # numbers out of the main buffer means selecting text never drags a
        # column of digits along with it.
        self.gutter = tk.Text(body, width=6, wrap='none', font=MONO,
                              background='#f0f0f0', takefocus=0,
                              state='disabled', cursor='arrow')

        self.gutter.grid(row=0, column=0, sticky='ns')
        self.text.grid(row=0, column=1, sticky='nsew')
        vsb.grid(row=0, column=2, sticky='ns')
        hsb.grid(row=1, column=1, sticky='ew')
        body.rowconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)

        self.text.tag_configure('hl', background=HL_BG, foreground=HL_FG)
        self.text.tag_configure('cur', background=CUR_BG)
        # Read-only without disabling: disabling would kill Ctrl+C copying.
        self.text.bind('<Key>', self._on_key)

        self.info_var = tk.StringVar(value='')
        info = ttk.Label(self, textvariable=self.info_var, anchor='w')
        info.pack(fill='x')

    def _on_text_scroll(self, first, last):
        # Keep the scrollbar and the line-number gutter in step with the text.
        # Without the gutter sync the numbers drift away from their lines as
        # soon as anyone scrolls -- which a jump makes unavoidable.
        self._vsb.set(first, last)
        self.gutter.yview_moveto(float(first))

    def _on_key(self, event):
        # Allow navigation and copy; block edits so the view stays a faithful
        # picture of the file on disk.
        if event.state & 4 and event.keysym.lower() in ('c', 'a'):
            return None
        if event.keysym in ('Up', 'Down', 'Left', 'Right', 'Prior', 'Next',
                            'Home', 'End'):
            return None
        return 'break'

    # ------------------------------------------------------------------- api

    def clear(self):
        self._entry = None
        self._lines = []
        self._keyword = ''
        self._match_case = False
        self._whole_word = False
        self._hit_lines = []
        self._total_hits = 0
        self._hit_rows = []
        self._cur_row = None
        self._jump_note = ''
        self._info_state = (0, 0, False, [], 0)
        self._load_token += 1
        self.text.configure(state='normal')
        self.text.delete('1.0', 'end')
        self.text.tag_remove('cur', '1.0', 'end')
        self._set_gutter([])
        self.info_var.set('')

    def show(self, fh, keyword, match_case=False, whole_word=False):
        """Display one file's content for the given search result.

        'Matched lines' comes from fh.hits -- the search's own result -- rather
        than re-running the keyword here. Re-deriving it would disagree with
        the results row whenever the search capped its hits, and would show
        nothing at all for a row selected from an earlier search whose options
        have since changed. `match_case` / `whole_word` only shape the
        highlight, which is presentation rather than membership.
        """
        self._keyword = keyword or ''
        self._match_case = match_case
        self._whole_word = whole_word
        self._hit_lines = [h.line_no - 1 for h in fh.hits]
        self._total_hits = fh.total_hits
        # A new file means the old jump target refers to different rows; drop
        # it before either the error path or the reload can be reached.
        self._reset_jump()
        if fh.error:
            self._entry = None
            self._lines = []
            self.text.configure(state='normal')
            self.text.delete('1.0', 'end')
            self.text.insert('end', 'unreadable: %s\n' % fh.error)
            self._set_gutter([])
            self.info_var.set('')
            return

        self._entry = fh.entry
        self._load_token += 1
        token = self._load_token
        self.text.configure(state='normal')
        self.text.delete('1.0', 'end')
        self.text.insert('end', 'loading…')
        self.text.configure(state='disabled')
        self._set_gutter([])
        # Reading is milliseconds even for the largest file here, but defer it
        # so the row selection paints immediately.
        self.after(0, lambda: self._load(token))

    def _load(self, token):
        if token != self._load_token or self._entry is None:
            return
        try:
            lines, truncated = self.loader(self._entry)
        except OSError as e:
            self._lines = []
            self._truncated = False
            self.text.configure(state='normal')
            self.text.delete('1.0', 'end')
            self.text.insert('end', 'cannot read file: %s\n' % e)
            self.text.configure(state='disabled')
            self.info_var.set('')
            return
        self._lines = lines
        self._truncated = truncated
        self._render()

    def _clear_filter(self):
        self.filter_var.set('')
        self._rerender()

    def _rerender(self):
        if self._lines or self._entry is not None:
            self._render()

    # ------------------------------------------------------------- jumping

    def _reset_jump(self):
        """Forget the jump cursor, discarding the yellow highlight with it."""
        self._cur_row = None
        self._jump_note = ''
        self.text.tag_remove('cur', '1.0', 'end')

    def _prev_hit(self):
        self._jump(-1)

    def _next_hit(self):
        self._jump(+1)

    def _jump(self, step):
        """Move the cursor to the neighbouring highlighted line.

        Targets are the lines carrying a green highlight -- keyword and filter
        matches both -- restricted to what is actually on screen.
        """
        rows = self._hit_rows
        if not rows:
            self._set_jump_note('no highlighted lines to jump to')
            return
        if self._cur_row is None:
            target = rows[0] if step > 0 else rows[-1]
        else:
            pos = bisect.bisect_left(rows, self._cur_row)
            nxt = pos + step
            # Clamp rather than wrap: at the last hit the user should be able
            # to see they are at the end, not silently loop to the first.
            if nxt < 0 or nxt >= len(rows):
                self._set_jump_note('at the %s highlighted line' %
                                    ('last' if step > 0 else 'first'))
                return
            target = rows[nxt]
        self._goto_row(target, rows)

    def _goto_row(self, row, rows=None):
        self._cur_row = row
        line = '%d.0' % (row + 1)
        self.text.tag_remove('cur', '1.0', 'end')
        self.text.tag_add('cur', line, '%d.end' % (row + 1))
        # 'cur' is configured after 'hl', so it would otherwise paint over the
        # keyword; raising 'hl' keeps the keyword visible inside the yellow.
        self.text.tag_raise('hl')
        self.text.see(line)
        if rows is None:
            rows = self._hit_rows
        pos = bisect.bisect_left(rows, row)
        self._set_jump_note('highlight %d of %d' % (pos + 1, len(rows)))

    def _set_jump_note(self, note):
        self._jump_note = note
        self._refresh_info()

    # --------------------------------------------------------------- render

    def _terms(self):
        return parse_terms(self.filter_var.get())

    def _render(self):
        lines = self._lines
        case = self.case_var.get()
        terms = self._terms()
        all_mode = self.mode_var.get() == 'all'

        # The base set is the hit list the search itself reported, so the pane
        # and the results row always agree on what 'matched' means. Filtering
        # is the only thing recomputed here.
        keep = None if all_mode else self._hit_lines

        chosen = select_lines(lines, terms, case, keep)

        # Highlight the original search keyword as well as the filter terms:
        # both are 'what the user is looking for', and leaving the keyword
        # unhighlighted in the file view would read as a regression when
        # switching from matched-only to the full text.
        kw_pat = keyword_pattern(self._keyword, self._match_case,
                                 self._whole_word)
        hl_patterns = [p for p in (kw_pat,) if p is not None]

        self.text.configure(state='normal')
        self.text.delete('1.0', 'end')
        # One insert for the whole visible block, then one tag_add carrying
        # every range: per-line insertion and per-match tagging both cost far
        # more, and this file can be several thousand lines.
        blob_parts = []
        ranges = []
        # Rows the jump buttons can land on: exactly the lines carrying a green
        # highlight, so every jump target is visibly marked. That means the
        # filter's matches count too, not just the search keyword's -- both are
        # highlighted, so both are things the user wants to step through.
        # Derived from the displayed rows rather than from _hit_lines, because
        # the latter is capped at max_hits_per_file and would stop jumping
        # after 500 hits in 'All file text'.
        self._hit_rows = []
        for row, (idx, line) in enumerate(chosen):
            if row:
                blob_parts.append('\n')
            blob_parts.append(line)
            spans = match_ranges(line, terms, case, hl_patterns)
            if spans:
                # Same spans that get tagged 'hl' below, so the jump set and
                # the green set cannot drift apart.
                self._hit_rows.append(row)
            for s, e in spans:
                ranges.append('%d.%d' % (row + 1, s))
                ranges.append('%d.%d' % (row + 1, e))
        self.text.insert('end', ''.join(blob_parts))
        if ranges:
            self.text.tag_add('hl', *ranges)
        self.text.configure(state='disabled')
        self.text.yview_moveto(0)
        # Row numbering changed, so any previous jump target is meaningless.
        self._cur_row = None
        self._jump_note = ''

        self._set_gutter([idx + 1 for idx, _ in chosen])
        hit_set = set(self._hit_lines)
        n_hits_shown = sum(1 for idx, _ in chosen if idx in hit_set)
        self._info_state = (len(chosen), len(lines), all_mode, terms,
                            n_hits_shown)
        self._refresh_info()

    def _set_gutter(self, numbers):
        self.gutter.configure(state='normal')
        self.gutter.delete('1.0', 'end')
        if numbers:
            self.gutter.insert('end', '\n'.join('%d' % n for n in numbers))
        self.gutter.configure(state='disabled')
        # Match the text view's current offset instead of forcing 0: the
        # scroll handler owns this position, and resetting it here would
        # desync the numbers from the lines after any scroll.

    def _refresh_info(self):
        shown, total, all_mode, terms, n_hits_shown = self._info_state
        if all_mode:
            base = 'all %d lines' % total
        else:
            base = '%d matched line%s' % (self._total_hits,
                                          '' if self._total_hits == 1 else 's')
            # The search stops collecting at max_hits_per_file; say so rather
            # than letting the pane look like it holds every hit.
            if self._total_hits > len(self._hit_lines):
                base += ' (first %d listed)' % len(self._hit_lines)
        bits = ['showing %d of %s' % (shown, base)]
        if terms:
            # The filter's role differs by mode: it adds lines to the hit set
            # in 'matched lines', and only marks them in 'all file text'. Say
            # which, so 'showing 6205 of all 6205 lines' next to a filter does
            # not look like the filter was ignored.
            if all_mode:
                bits.append('filter: %s (highlight only)' % ' OR '.join(terms))
            else:
                bits.append('filter: %s (+%d non-hit lines)' % (
                    ' OR '.join(terms), shown - n_hits_shown))
        if self._truncated:
            bits.append('file truncated for display')
        if not all_mode and not terms:
            bits.append("switch to 'All file text' to see the rest")
        if self._jump_note:
            bits.append(self._jump_note)
        self.info_var.set(' · '.join(bits))

