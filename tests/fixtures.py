import gzip
import io
import os
import shutil
import struct
import zipfile


def build_zip(entries, compression=zipfile.ZIP_DEFLATED):
    """Build an in-memory zip from {name: bytes} or [(name, bytes, flag_utf8)]."""
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, 'w', compression) as z:
        for item in entries:
            if isinstance(item, tuple):
                name, data = item[0], item[1]
            else:
                name, data = item, entries[item]
            if isinstance(name, str):
                z.writestr(name, data)
            else:
                z.writestr(name, data)
    return bio.getvalue()


def build_zip_raw_gbk_name(gbk_name_bytes: bytes, data: bytes):
    """Craft a zip whose entry name is raw GBK with the UTF-8 flag CLEAR.

    zipfile cannot be asked to do this, so the local header and central
    directory are assembled by hand. This is the exact shape produced by older
    Windows archivers on a Chinese system -- the case decode_entry_name fixes.
    """
    crc = zipfile.crc32(data) & 0xffffffff
    comp = zipfile.ZipFile(io.BytesIO(), 'w')  # only for compress helper
    import zlib
    compressed = zlib.compressobj(6, zlib.DEFLATED, -15)
    cdata = compressed.compress(data) + compressed.flush()
    csize, usize = len(cdata), len(data)

    local = struct.pack('<IHHHHHIIIHH', 0x04034b50, 20, 0, 8, 0, 0,
                        crc, csize, usize, len(gbk_name_bytes), 0)
    local += gbk_name_bytes + cdata

    central = struct.pack('<IHHHHHHIIIHHHHHII', 0x02014b50, 20, 20, 0, 8, 0, 0,
                          crc, csize, usize, len(gbk_name_bytes), 0, 0, 0, 0,
                          0, 0)
    central += gbk_name_bytes
    cd_offset = len(local)
    end = struct.pack('<IHHHHIIH', 0x06054b50, 0, 0, 1, 1, len(central), cd_offset, 0)
    return local + central + end


def nested_zip(depth: int, leaf_name: str, leaf_data: bytes) -> bytes:
    """Build a zip nested `depth` levels deep with one leaf at the bottom."""
    payload = build_zip({leaf_name: leaf_data})
    for i in range(depth - 1):
        payload = build_zip({'level%d.zip' % i: payload})
    return payload


def bomb_bytes(size: int = 2 << 20) -> bytes:
    """Highly compressible payload to exercise the ratio guard."""
    return b'\x00' * size


def write(path: str, data: bytes) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)
    return path


def rmtree(path: str):
    shutil.rmtree(path, ignore_errors=True)
