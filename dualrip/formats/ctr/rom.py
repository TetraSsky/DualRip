"""3DS ROM access: a built-in reader for decrypted images, pyctr for encrypted ones."""

from __future__ import annotations

import os
import struct

MEDIA_UNIT = 0x200

class Boot9RequiredError(RuntimeError):
    """Raised when opening an encrypted ROM with no boot ROM file set."""

    def __init__(self, rom_path):
        super().__init__(f'{os.path.basename(rom_path)} is encrypted and no boot ROM (boot9.bin) is set in Settings.')

def _align(value, unit):
    return (value + unit - 1) // unit * unit

class _FileWindow:
    """Read-only view of a byte range of a file, with its own handle."""

    def __init__(self, path, offset, size):
        self._f = open(path, 'rb')
        self._offset = offset
        self._size = size
        self._pos = 0

    def read(self, n=-1):
        remaining = self._size - self._pos
        if n < 0 or n > remaining:
            n = remaining
        self._f.seek(self._offset + self._pos)
        data = self._f.read(n)
        self._pos += len(data)
        return data

    def seek(self, pos, whence=0):
        if whence == 1:
            pos += self._pos
        elif whence == 2:
            pos += self._size
        self._pos = max(0, pos)
        return self._pos

    def tell(self):
        return self._pos

    def close(self):
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

class PlainRomFS:
    """RomFS reader for a decrypted image region."""

    def __init__(self, rom_path, region_offset):
        self._rom_path = rom_path
        with open(rom_path, 'rb') as f:
            f.seek(region_offset)
            header = f.read(0x60)
            lv3_offset = region_offset
            if header[0:4] == b'IVFC':
                magic_num, master_hash_size = struct.unpack_from('<II', header, 4)
                if magic_num != 0x10000:
                    raise ValueError('bad IVFC magic number 0x%X' % magic_num)
                lv3_block = 1 << struct.unpack_from('<I', header, 0x4C)[0]
                lv3_offset += _align(0x60 + master_hash_size, lv3_block)
                f.seek(lv3_offset)
            lv3 = struct.unpack('<10I', f.read(0x28))
            (header_size, _dh_off, _dh_size, dm_off, dm_size,
             _fh_off, _fh_size, fm_off, fm_size, data_off) = lv3
            if header_size != 0x28:
                raise ValueError('bad RomFS level 3 header size')
            self._data_offset = lv3_offset + data_off
            f.seek(lv3_offset + dm_off)
            dirmeta = f.read(dm_size)
            f.seek(lv3_offset + fm_off)
            filemeta = f.read(fm_size)

        # path (lowercased) -> ('dir', name, children) or ('file', name, offset, size)
        self._nodes = {}
        self._walk_dir(dirmeta, filemeta, 0, '', 'ROOT')

    def _walk_dir(self, dirmeta, filemeta, entry_offset, path, name):
        (_parent, _sibling, first_dir, first_file, _hash_next,
         name_len) = struct.unpack_from('<6I', dirmeta, entry_offset)
        contents = []
        self._nodes[path.lower() or '/'] = ('dir', name, contents)
        offset = first_dir
        while offset != 0xFFFFFFFF:
            sibling, = struct.unpack_from('<I', dirmeta, offset + 0x4)
            child_len, = struct.unpack_from('<I', dirmeta, offset + 0x14)
            child = dirmeta[offset + 0x18:offset + 0x18 + child_len].decode('utf-16le')
            contents.append(child)
            self._walk_dir(dirmeta, filemeta, offset, path + '/' + child, child)
            offset = sibling
        offset = first_file
        while offset != 0xFFFFFFFF:
            (_parent, sibling, data_offset, size, _hash_next,
             child_len) = struct.unpack_from('<IIQQII', filemeta, offset)
            child = filemeta[offset + 0x20:offset + 0x20 + child_len].decode('utf-16le')
            contents.append(child)
            self._nodes[(path + '/' + child).lower()] = ('file', child, data_offset, size)
            offset = sibling

    def _node(self, path):
        key = '/' + path.strip('/').lower() if path.strip('/') else '/'
        try:
            return self._nodes[key]
        except KeyError:
            raise FileNotFoundError(path)

    def get_info_from_path(self, path):
        node = self._node(path)
        if node[0] == 'dir':
            return _DirEntry(node[1], tuple(node[2]))
        return _FileEntry(node[1], node[2], node[3])

    def open(self, path):
        node = self._node(path)
        if node[0] != 'file':
            raise IsADirectoryError(path)
        return _FileWindow(self._rom_path, self._data_offset + node[2], node[3])

class _DirEntry:
    type = 'dir'

    def __init__(self, name, contents):
        self.name = name
        self.contents = contents

class _FileEntry:
    type = 'file'

    def __init__(self, name, offset, size):
        self.name = name
        self.offset = offset
        self.size = size

class PlainRomReader:
    """Container access for a decrypted .cia or .3ds image."""

    def __init__(self, rom_path, romfs):
        self.rom_path = rom_path
        self.romfs = romfs

    def close(self):
        pass

def _cia_content0(rom_path):
    """Offset of the first content of a .cia, or None if the CIA layer is encrypted."""
    with open(rom_path, 'rb') as f:
        (header_size, _type, _version, cert_size, ticket_size, tmd_size,
         _meta_size, _content_size) = struct.unpack('<IHHIIIIQ', f.read(0x20))
        tmd_offset = _align(header_size, 0x40) + _align(cert_size, 0x40) + _align(ticket_size, 0x40)
        f.seek(tmd_offset)
        tmd = f.read(tmd_size)
    sig_type, = struct.unpack_from('>I', tmd)
    sig_sizes = {0x10000: (0x200, 0x3C), 0x10001: (0x100, 0x3C), 0x10002: (0x3C, 0x40),
                 0x10003: (0x200, 0x3C), 0x10004: (0x100, 0x3C), 0x10005: (0x3C, 0x40)}
    sig_size, pad = sig_sizes[sig_type]
    base = 4 + sig_size + pad
    record = base + 0xC4 + 0x24 * 64
    _cid, _cindex, ctype = struct.unpack_from('>IHH', tmd, record)
    if ctype & 1:
        return None
    return _align(tmd_offset + tmd_size, 0x40)

def _plain_romfs_region(rom_path):
    """RomFS byte offset of a decrypted image, or None when keys are needed."""
    ext = os.path.splitext(rom_path)[1].lower()
    if ext == '.cia':
        ncch_offset = _cia_content0(rom_path)
        if ncch_offset is None:
            return None
    else:
        with open(rom_path, 'rb') as f:
            header = f.read(0x200)
        if header[0x100:0x104] != b'NCSD':
            raise ValueError(f'not a 3DS ROM: {rom_path!r}')
        part_offset, part_size = struct.unpack_from('<II', header, 0x120)
        if part_size == 0:
            raise ValueError(f'empty first partition: {rom_path!r}')
        ncch_offset = part_offset * MEDIA_UNIT
    with open(rom_path, 'rb') as f:
        f.seek(ncch_offset)
        ncch = f.read(0x200)
    if ncch[0x100:0x104] != b'NCCH':
        raise ValueError(f'no NCCH container found in {rom_path!r}')
    flags = ncch[0x188:0x190]
    if not flags[7] & 0x4:
        return None
    romfs_units, = struct.unpack_from('<I', ncch, 0x1B0)
    if romfs_units == 0:
        raise ValueError(f'no RomFS in {rom_path!r}')
    return ncch_offset + romfs_units * MEDIA_UNIT

def _open_encrypted(rom_path, boot9):
    """Open an encrypted ROM with an explicit boot ROM file."""
    try:
        from pyctr.crypto import CryptoEngine
        from pyctr.type.cia import CIAReader
        from pyctr.type.cci import CCIReader
    except ImportError:
        raise RuntimeError('pyctr is required to open encrypted 3DS ROMs: pip install pyctr')
    try:
        CryptoEngine(boot9=boot9)
    except Exception as exc:
        raise RuntimeError(f'Cannot load boot ROM file {boot9!r}: {exc}')
    ext = os.path.splitext(rom_path)[1].lower()
    reader = CIAReader(rom_path) if ext == '.cia' else CCIReader(rom_path)
    return reader, reader.contents[0].romfs

def open_romfs(rom_path, boot9=None):
    """Open a .cia/.3ds ROM, returns (reader, romfs)."""
    region = _plain_romfs_region(rom_path)
    if region is not None:
        romfs = PlainRomFS(rom_path, region)
        return PlainRomReader(rom_path, romfs), romfs
    if not boot9:
        raise Boot9RequiredError(rom_path)
    return _open_encrypted(rom_path, boot9)

def romfs_walk(romfs, path='/'):
    entry = romfs.get_info_from_path(path)
    if entry.type == 'dir':
        for name in entry.contents:
            yield from romfs_walk(romfs, path.rstrip('/') + '/' + name)
    else:
        yield path
