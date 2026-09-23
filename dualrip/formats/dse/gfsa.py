"""GFSA container reader — unpacks embedded DSE (SWDL/SMDL/SEDL) chunks."""

import struct
import ndspy.lz10

DSE_MAGICS = (b'swdl', b'smdl', b'sedl')

class _Rdr:
    def __init__(self, data):
        self.data = data
        self.c = 0

    def u8(self):
        v = self.data[self.c]
        self.c += 1
        return v

    def u8n(self):
        if self.c >= len(self.data):
            return None
        return self.u8()

    def u24(self):
        v = int.from_bytes(self.data[self.c:self.c + 3], 'little')
        self.c += 3
        return v

    def u32(self):
        v = int.from_bytes(self.data[self.c:self.c + 4], 'little')
        self.c += 4
        return v

    def read(self, n):
        v = self.data[self.c:self.c + n]
        self.c += n
        return v

class _HNode:
    __slots__ = ('is_data', 'data', 'child0', 'child1')

    def __init__(self, is_data):
        self.is_data = is_data
        self.data = None
        self.child0 = None
        self.child1 = None

def _huff_tree(rdr, is_data, rel_off, max_pos):
    node = _HNode(is_data)
    if rdr.c >= max_pos:
        return None
    node.data = rdr.u8()
    if not is_data:
        offset = node.data & 0x3F
        zero_is_data = (node.data & 0x80) > 0
        one_is_data = (node.data & 0x40) > 0
        zero_rel = (rel_off ^ (rel_off & 1)) + offset * 2 + 2
        curr = rdr.c
        rdr.c += (zero_rel - rel_off) - 1
        node.child0 = _huff_tree(rdr, zero_is_data, zero_rel, max_pos)
        node.child1 = _huff_tree(rdr, one_is_data, zero_rel + 1, max_pos)
        rdr.c = curr
    return node

def _huff_decompress(data):
    rdr = _Rdr(data)
    blocksize = 4 if rdr.u8() == 0x24 else 8
    ds = rdr.u24()
    if ds == 0:
        ds = rdr.u32()
    treesize = (rdr.u8() + 1) * 2
    tree_end = (rdr.c - 1) + treesize
    root = _huff_tree(rdr, False, 5, tree_end)
    rdr.c = tree_end
    out = bytearray()
    bitsleft = 0
    word = 0
    cached = -1
    cur = root
    while len(out) < ds:
        while not cur.is_data:
            if bitsleft == 0:
                word = rdr.u32()
                bitsleft = 32
            bitsleft -= 1
            cur = cur.child1 if (word & (1 << bitsleft)) else cur.child0
        if blocksize == 8:
            out.append(cur.data)
        elif cached < 0:
            cached = cur.data
        else:
            out.append(cached | (cur.data << 4))
            cached = -1
        cur = root
    return bytes(out)

def _rle_decompress(data, ds):
    rdr = _Rdr(data)
    rdr.u8() # 0x30
    rdr.u24()
    out = bytearray()
    while len(out) < ds:
        flag = rdr.u8n()
        if flag is None:
            break
        compressed = (flag & 0x80) > 0
        length = (flag & 0x7F) + (3 if compressed else 1)
        if compressed:
            out.extend(bytes([rdr.u8()]) * length)
        else:
            out.extend(rdr.read(length))
    return bytes(out)

def _std_header(type_byte, usize):
    if usize < 0x1000000:
        return bytes([type_byte]) + usize.to_bytes(3, 'little')
    return bytes([type_byte, 0, 0, 0]) + usize.to_bytes(4, 'little')

def decompress(raw):
    """Decompress a GFSA control-word-prefixed block or file."""
    cw = int.from_bytes(raw[:4], 'little')
    algo = cw & 7
    usize = cw >> 3
    payload = raw[4:]
    if algo == 0:
        return payload[:usize]
    if algo == 1:
        return ndspy.lz10.decompress(_std_header(0x10, usize) + payload)
    if algo == 2:
        return _huff_decompress(_std_header(0x24, usize) + payload)
    if algo == 3:
        return _huff_decompress(_std_header(0x28, usize) + payload)
    if algo == 4:
        return _rle_decompress(_std_header(0x30, usize) + payload, usize)
    raise ValueError('unknown compression algo %d' % algo)

class Gfsa:
    """Parsed GFSA archive."""

    def __init__(self, data):
        if data[:4] != b'GFSA':
            raise ValueError('not a GFSA archive (magic %r)' % data[:4])
        self.data = data
        (self.blk1, self.blk2, self.blk3, self.blk4, self.data_ptr,
         self.folder_count, self.file_count) = struct.unpack_from('<7I', data, 0x04)
        self.fat = decompress(data[self.blk2:self.blk3])

    def files(self):
        """Yield (index, archive_offset, size) for each FAT entry."""
        for i in range(self.file_count):
            e = i * 8
            off_lo, size_lo, packed = struct.unpack_from('<HHH', self.fat, e + 2)
            offset = (off_lo | ((packed & 0x0FFF) << 16)) * 4 + self.data_ptr
            size = size_lo | (((packed >> 12) & 0x7) << 16)
            yield i, offset, size

    def raw(self, offset, size):
        return self.data[offset:offset + size]

def carve_dse(blob):
    """Yield (magic, bytes) for each SWDL/SMDL/SEDL embedded in a blob."""
    for mg in DSE_MAGICS:
        start = 0
        while True:
            i = blob.find(mg, start)
            if i < 0:
                break
            start = i + 4
            if i + 0x10 > len(blob):
                continue
            flen = struct.unpack_from('<I', blob, i + 8)[0]
            version = struct.unpack_from('<H', blob, i + 0x0C)[0]
            if version == 0x0415 and 0x40 < flen <= len(blob) - i:
                yield mg.decode('ascii'), blob[i:i + flen]

def _chunk_name(mg, content):
    name = content[0x20:0x30].split(b'\x00')[0].decode('ascii', 'replace').strip()
    name = ''.join(c if c.isalnum() or c in '._-' else '_' for c in name)
    return name or mg

def extract_dse_files(archive_bytes):
    """Yield (member_name, chunk_bytes) for every DSE chunk in a GFSA archive."""
    gfsa = Gfsa(archive_bytes)
    for i, offset, size in gfsa.files():
        raw = gfsa.raw(offset, size)
        if len(raw) < 4:
            continue
        blob = raw if raw[:4] in DSE_MAGICS + (b'GFSP',) else None
        if blob is None:
            try:
                dec = decompress(raw)
            except Exception:
                continue
            if dec[:4] in DSE_MAGICS + (b'GFSP',):
                blob = dec
        if blob is None:
            continue
        for j, (mg, content) in enumerate(carve_dse(blob)):
            yield '%04d_%d_%s' % (i, j, _chunk_name(mg, content)), content
