import re
import zipfile

_ALT_CODECS = ('utf-8', 'gbk', 'big5')

# Characters no mainstream filesystem accepts. Also covers the ASCII control
# range, which Tk's save dialog would otherwise pass straight through.
_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_STEM = 80


def decode_entry_name(info: zipfile.ZipInfo) -> str:
    """Best-effort human-readable member name. Never raises.

    zipfile decodes ZipInfo.filename as cp437 when the UTF-8 flag (0x800) is
    clear. That flag is unreliable: some Windows archivers write GBK bytes with
    the flag clear. Encoding back to cp437 recovers the original bytes, which we
    then re-decode with the likely codecs.
    """
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        raw = name.encode('cp437')
    except UnicodeEncodeError:
        # Not cp437-representable, so it did not come through the cp437 path.
        return name
    for codec in _ALT_CODECS:
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            continue
    return name


def repair_surrogates(name: str) -> str:
    """Replace lone surrogates (tarfile's surrogateescape output) with U+FFFD.

    Keeps the string printable and JSON-serialisable instead of raising at the
    encode boundary much later.
    """
    try:
        name.encode('utf-8')
        return name
    except UnicodeEncodeError:
        return name.encode('utf-8', 'surrogateescape').decode('utf-8', 'replace')


def join_vp(prefix: str, name: str) -> str:
    """Append a member name to a virtual path, normalising separators.

    Archive members use '/' regardless of platform; the leading '/' of an
    absolute member name is stripped so the vp never looks like a rooted path.
    """
    name = name.replace('\\', '/').lstrip('/')
    if not prefix:
        return name
    return prefix + '/' + name


def export_stem(vp: str) -> str:
    """A safe, recognisable default filename stem for exporting one member.

    The virtual path is unusable as a filename twice over: it is hundreds of
    characters of nested archive names, and it contains characters Windows
    rejects outright (':' from drive-free archive names, '|', control bytes
    from a mis-decoded member). So this keeps only the last two segments --
    enough to tell 'hilog/el2.log' from 'kmsg/el2.log' -- drops the member's
    own extension, strips anything a filesystem would refuse, and caps the
    length so the save dialog opens without the name scrolling off screen.

    'a.zip/.../Eventid_x_rda_el2.zip/hilog/el2.log' -> 'hilog_el2'
    """
    parts = [p for p in vp.split('/') if p and p not in ('.', '..')]
    if not parts:
        return 'export'
    # Every export is written as .txt, so keeping the member's own extension
    # here would only produce names like 'el2.log.txt'. rpartition rather than
    # splitext so a name that is nothing but an extension ('.log') survives
    # intact instead of collapsing to an empty stem.
    head, dot, _ext = parts[-1].rpartition('.')
    if dot and head:
        parts[-1] = head
    stem = '_'.join(parts[-2:]) if len(parts) > 1 else parts[0]
    stem = _UNSAFE_FILENAME.sub('_', stem).strip(' .')
    # Collapse the runs of '_' the substitution above can leave behind.
    stem = re.sub(r'_{2,}', '_', stem)
    if len(stem) > _MAX_STEM:
        # Trim the middle rather than the tail: the tail carries the line of
        # the virtual path that actually identifies the file.
        keep = _MAX_STEM - 1
        front = keep // 2
        stem = stem[:front] + '~' + stem[-(keep - front):]
    return stem or 'export'


def shorten_vp(vp: str, maxlen: int = 120) -> str:
    """Elide the middle of a virtual path, keeping the root archive and the tail.

    'Log_X.zip/CLUSTER/cluster/CDC_..._61143_N.zip/Eventid_..._rda.zip/hilog/k.log'
      -> 'Log_X.zip/…/Eventid_..._rda.zip/hilog/k.log'

    The full path is always available in the detail pane; this is display only.
    """
    if len(vp) <= maxlen:
        return vp
    parts = vp.split('/')
    if len(parts) <= 2:
        return vp[:maxlen - 1] + '…'
    head = parts[0]
    # Grow the tail one segment at a time until we run out of budget.
    tail = []
    for seg in reversed(parts[1:]):
        candidate = '…/' + '/'.join([seg] + tail)
        if len(head) + 1 + len(candidate) > maxlen:
            break
        tail.insert(0, seg)
    if not tail:
        return head + '/…'
    return head + '/…/' + '/'.join(tail)
