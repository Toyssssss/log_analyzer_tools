SAMPLE_SIZE = 64 * 1024
MAX_LINE_CHARS = 2000
BINARY_PROBE = 8 * 1024


def detect_codec(sample: bytes) -> str:
    """Pick a display codec from a file sample.

    UTF-8 is tried first: GBK text essentially never validates as UTF-8, while
    the reverse happens by accident often, so this order routes ASCII/UTF-8
    logs correctly and sends the rest to GBK. Callers decode with
    errors='replace', so a wrong guess costs a few U+FFFD, never an exception.
    """
    try:
        sample.decode('utf-8')
        return 'utf-8'
    except UnicodeDecodeError:
        return 'gbk'


def decode_line(raw: bytes, codec: str) -> str:
    """Decode one line for display. Never raises.

    Only the trailing newline/CR is removed -- leading whitespace in hilog is
    field alignment and stripping it destroys the column structure that makes
    the timestamp/pid/tag readable.
    """
    text = raw.decode(codec, errors='replace')
    text = text.rstrip('\r\n')
    if len(text) > MAX_LINE_CHARS:
        return text[:MAX_LINE_CHARS] + '…'
    return text


def is_binary_chunk(sample: bytes) -> bool:
    """True when the sample looks like binary (contains NUL).

    Used only to badge/filter files in the UI. Binary files stay searchable by
    default: kmsg logs and embedded crash dumps are exactly where keyword
    search earns its keep, and skipping them would be a silent false negative.
    """
    return b'\x00' in sample


def read_sample(fh, size: int = SAMPLE_SIZE) -> bytes:
    """Read a prefix without disturbing the caller's position assumptions."""
    pos = fh.tell()
    try:
        return fh.read(size)
    finally:
        fh.seek(pos)
