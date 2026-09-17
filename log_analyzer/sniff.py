ARCHIVE_EXTENSIONS = (
    '.zip', '.tar', '.tgz', '.tbz', '.tbz2', '.txz',
    '.tar.gz', '.tar.bz2', '.tar.xz',
    '.gz', '.bz2', '.xz', '.7z', '.rar',
)

_MAGIC_HEAD = 512


def looks_like_archive_name(name: str) -> bool:
    """Cheap extension pre-filter.

    Retained from the original scaffold as a fast path that avoids a read for
    the overwhelming majority of entries (the .log leaves). It is NOT the
    decision procedure -- sniff() is.
    """
    return name.lower().endswith(ARCHIVE_EXTENSIONS)


def sniff(head: bytes) -> str:
    """Classify an archive by magic bytes. Returns a kind name or None.

    Does not open files or construct parser objects, unlike
    tarfile.is_tarfile() which opens and fully parses on every probe.
    """
    if len(head) >= 4 and head[:4] in (b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08'):
        return 'zip'
    if head[:2] == b'\x1f\x8b':
        return 'gzip'
    if head[:3] == b'BZh':
        return 'bzip2'
    if head[:6] == b'\xfd7zXZ\x00':
        return 'xz'
    if len(head) >= _MAGIC_HEAD and head[257:262] in (b'ustar', b'ustar\x00'):
        return 'tar'
    if head[:6] == b'7z\xbc\xaf\x27\x1c':
        return '7z'
    if head[:7] == b'Rar!\x1a\x07\x00' or head[:7] == b'Rar!\x1a\x07\x01':
        return 'rar'
    return None


def sniff_file(path: str) -> str:
    """sniff() against a file on disk. Returns None when unreadable."""
    from .paths import win_long
    try:
        with open(win_long(path), 'rb') as f:
            return sniff(f.read(_MAGIC_HEAD))
    except OSError:
        return None
