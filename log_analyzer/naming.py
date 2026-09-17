import zipfile

_ALT_CODECS = ('utf-8', 'gbk', 'big5')


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
