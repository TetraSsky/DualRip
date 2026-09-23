"""SEDL parsing (DSE): sequenced sound effects."""

import struct

_SONG_TPQN_OFFSET = 0x30 # the param block sits at (first trk - 0x30); tpqn at +2

def _find_all(data, token):
    out, i = [], 0
    while True:
        j = data.find(token, i)
        if j < 0:
            break
        out.append(j)
        i = j + 1
    return out

def _valid_chunk(data, off, magic):
    """A real 4-aligned DSE chunk with an in-bounds length, not a byte coincidence."""
    if data[off:off + 4] != magic or (off & 3):
        return False
    clen = struct.unpack_from('<I', data, off + 0x0C)[0]
    return 0 <= clen and off + 0x10 + clen <= len(data)

def _tpqn_before(data, trk_off):
    """Return the TPQN value from the param block before a trk chunk, or 48 if not found."""
    lo = max(0x40, trk_off - 0x40)
    for o in range(trk_off - 6, lo - 1, -1):
        if data[o] == 1 and data[o + 1] == 0 and data[o + 4] == 1 and data[o + 5] == 0xFF:
            return struct.unpack_from('<H', data, o + 2)[0]
    return 48

def parse_sedl(data):
    """[(tpqn, [track_event_bytes, ...]), ...] — one entry per SFX sequence."""
    if data[:4] != b'sedl':
        raise ValueError('not a SEDL')
    marks = sorted([(o, 'trk') for o in _find_all(data, b'trk ') if _valid_chunk(data, o, b'trk ')] + [(o, 'eoc') for o in _find_all(data, b'eoc ') if _valid_chunk(data, o, b'eoc ')])
    seqs = []
    cur, tpqn = [], 48
    for off, kind in marks:
        if kind == 'trk':
            if not cur:
                tpqn = _tpqn_before(data, off)
            clen = struct.unpack_from('<I', data, off + 0x0C)[0]
            cur.append(data[off + 0x14: off + 0x10 + clen]) # skip 0x10 header + 4-byte preamble
        elif cur:
            seqs.append((tpqn, cur))
            cur = []
    if cur:
        seqs.append((tpqn, cur))
    return seqs
