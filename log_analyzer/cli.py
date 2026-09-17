import argparse
import csv
import json
import os
import sys

from . import __version__
from .extractor import CancelToken, extract_archive, scan_archive
from .manifest import (
    clear_workspace, index_upsert, inspect_workspace, load_index, make_ref,
    scan_workspace,
)
from .paths import archive_key, human_bytes, workspace_root
from .search import SearchOptions, search
from .source import DiskSource


def _key_for(args, path):
    return archive_key(path)


def _prepare(args, ws_root, path, key):
    """Return a manifest, extracting first if the cache has nothing usable."""
    state, man, partial, done = inspect_workspace(ws_root, key)
    if state == 'complete' and man is not None and not args.force:
        return man, False
    if state == 'partial' and not args.force:
        print('workspace holds an interrupted extraction (%d parts, %d files); '
              'resuming' % (len(done), len(partial)), file=sys.stderr)
        resume = True
    else:
        resume = False

    structural = None
    if not resume:
        structural = scan_archive(path)
        print('archive expands to %s across %d files (%d archives)' % (
            human_bytes(structural.total_bytes), structural.files,
            structural.archives), file=sys.stderr)

    man = extract_archive(
        path, ws_root, key, resume=resume, force=args.force,
        structural=structural,
        log=lambda m: print('  ' + m, file=sys.stderr),
        progress=lambda ph, d, t, note: print(
            '\r  %s %d/%d %-40s' % (ph, d, t, note[:40]), end='', file=sys.stderr),
    )
    print('', file=sys.stderr)
    index_upsert(ws_root, make_ref(key, man))
    return man, True


def cmd_scan(args):
    s = scan_archive(args.archive)
    print('archives      : %d' % s.archives)
    print('files         : %d' % s.files)
    print('uncompressed  : %s (%d bytes)' % (human_bytes(s.total_bytes), s.total_bytes))
    print('max depth     : %d' % s.max_depth)
    print('top-level     : %d' % s.top_level_entries)
    for w in s.warnings[:20]:
        print('warning       : %s' % w)
    return 0


def cmd_extract(args):
    ws_root = workspace_root(args.workspace)
    path = os.path.abspath(args.archive)
    key = archive_key(path)
    man, did = _prepare(args, ws_root, path, key)
    action = 'extracted' if did else 'already cached'
    print('%s: %d files, %s, status=%s' % (
        action, len(man.entries), human_bytes(man.total_bytes), man.status))
    print('workspace: %s' % os.path.join(ws_root, 'archives', key))
    return 0


def cmd_search(args):
    ws_root = workspace_root(args.workspace)
    path = os.path.abspath(args.archive)
    key = archive_key(path)
    man, _did = _prepare(args, ws_root, path, key)
    if man.status != 'complete' and not args.partial:
        print('extraction is incomplete (%d/%s parts); pass --partial to search '
              'what is there' % (man.stats.get('completed_entries', 0),
                                 man.stats.get('top_level_entries', '?')),
              file=sys.stderr)
        return 2

    opts = SearchOptions(
        keyword=args.keyword, match_case=args.match_case,
        whole_word=args.whole_word, include_binary=not args.no_binary,
        name_filter=args.name_filter)
    source = DiskSource(ws_root, key)
    results = []

    def sink(batch):
        results.extend(batch)

    summary = search(man.entries, source, opts, cancel=CancelToken(), sink=sink)

    if args.json:
        out = [{
            'file': fh.entry.vp,
            'hits': fh.total_hits,
            'truncated': fh.truncated,
            'binary': fh.binary,
            'lines': [{'line_no': h.line_no, 'line': h.line} for h in fh.hits],
        } for fh in results]
        json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write('\n')
    elif args.csv:
        w = csv.writer(sys.stdout)
        w.writerow(['virtual_path', 'line_no', 'line'])
        for fh in results:
            for h in fh.hits:
                w.writerow([fh.entry.vp, h.line_no, h.line])
    else:
        for fh in results:
            extra = ''
            if fh.truncated:
                extra = '  (showing first %d of %d)' % (len(fh.hits), fh.total_hits)
            elif fh.total_hits > 1:
                extra = '  (%d hits)' % fh.total_hits
            print(fh.entry.vp + extra)
            if args.lines:
                for h in fh.hits:
                    print('    %6d  %s' % (h.line_no, h.line))

    print(summary.describe(), file=sys.stderr)
    return 0 if summary.matched_files else 1


def cmd_cache(args):
    ws_root = workspace_root(args.workspace)
    if args.action == 'list':
        info = scan_workspace(ws_root)
        if not info['archives']:
            print('workspace is empty (%s)' % ws_root)
            return 0
        for a in sorted(info['archives'], key=lambda x: x.get('display_name', '')):
            print('%-40s %8s  %6s files  %s' % (
                (a.get('display_name') or a.get('key'))[:40],
                human_bytes(a.get('bytes', 0)), a.get('files', '?'),
                a.get('status', '?')))
        print('total: %s in %d archives' % (
            human_bytes(info['total_bytes']), len(info['archives'])))
        return 0
    if args.action == 'clear':
        key = archive_key(args.archive) if args.archive else None
        clear_workspace(ws_root, key)
        print('cleared %s' % (key or 'all archives'))
        return 0
    return 1


def cmd_gui(args):
    from .gui.app import main as gui_main
    return gui_main(workspace=args.workspace)


def build_parser():
    p = argparse.ArgumentParser(
        prog='Cluster-log-analyzer',
        description='Recursive nested-archive log extractor and keyword search')
    p.add_argument('--version', action='version', version=__version__)
    p.add_argument('--workspace', help='workspace root (overrides env and default)')
    sub = p.add_subparsers(dest='cmd')

    s = sub.add_parser('gui', help='launch the graphical interface')
    s.set_defaults(func=cmd_gui)

    s = sub.add_parser('scan', help='report structure and size without extracting')
    s.add_argument('archive')
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser('extract', help='recursively extract into the workspace')
    s.add_argument('archive')
    s.add_argument('--force', action='store_true', help='re-extract from scratch')
    s.set_defaults(func=cmd_extract)

    s = sub.add_parser('search', help='search a keyword across an archive')
    s.add_argument('archive')
    s.add_argument('-k', '--keyword', required=True)
    s.add_argument('--match-case', action='store_true')
    s.add_argument('--whole-word', action='store_true')
    s.add_argument('--no-binary', action='store_true', help='skip binary files')
    s.add_argument('--name-filter', help='only files whose virtual path contains this')
    s.add_argument('--json', action='store_true')
    s.add_argument('--csv', action='store_true')
    s.add_argument('--lines', action='store_true', help='print matched lines')
    s.add_argument('--force', action='store_true', help='re-extract from scratch')
    s.add_argument('--partial', action='store_true',
                   help='search an incomplete extraction anyway')
    s.set_defaults(func=cmd_search)

    s = sub.add_parser('cache', help='inspect or clear the workspace')
    s.add_argument('action', choices=['list', 'clear'])
    s.add_argument('archive', nargs='?', help='with clear: only this archive')
    s.set_defaults(func=cmd_cache)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, 'cmd', None):
        # No arguments at all is the common double-click case: show the GUI.
        if argv is None and len(sys.argv) == 1:
            return cmd_gui(args)
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print('interrupted', file=sys.stderr)
        return 130
    except ValueError as e:
        print('error: %s' % e, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
