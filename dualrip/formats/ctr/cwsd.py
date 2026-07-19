"""3DS CWSD wave-sound parsing."""

import struct

class WsdNote:
    """One note in a wave-sound item."""
    __slots__ = ('wave_index', 'org_key', 'volume', 'pan', 'pitch', 'attack', 'decay', 'sustain', 'hold', 'release')

class WsdItem:
    """One wave-sound item."""
    __slots__ = ('pan', 'pitch', 'events', 'notes')

class Cwsd:
    """Parsed CWSD wave table and sound items."""

    def __init__(self, data):
        if data[:4] != b'CWSD':
            raise ValueError('bad CWSD magic %r' % data[:4])
        self.data = data
        nsec, = struct.unpack_from('<H', data, 0x10)
        info = None
        for i in range(nsec):
            sid, _, off, size = struct.unpack_from('<HHII', data, 0x14 + i * 0x0C)
            if data[off:off + 4] == b'INFO':
                info = off
        base = info + 8
        _, _, woff = struct.unpack_from('<HHi', data, base)
        _, _, ioff = struct.unpack_from('<HHi', data, base + 8)
        self.waves = []
        if woff != -1:
            tbl = base + woff
            count, = struct.unpack_from('<I', data, tbl)
            for i in range(count):
                war_id, wav_idx = struct.unpack_from('<II', data, tbl + 4 + i * 8)
                self.waves.append((war_id & 0xFFFFFF, wav_idx))
        self.items = []
        if ioff != -1:
            tbl = base + ioff
            count, = struct.unpack_from('<I', data, tbl)
            for i in range(count):
                rid, _, roff = struct.unpack_from('<HHi', data, tbl + 4 + i * 8)
                self.items.append(None if roff == -1 else self._read_item(tbl + roff))

    def _flag_values(self, off):
        flags, = struct.unpack_from('<I', self.data, off)
        vals = {}
        pos = off + 4
        for bit in range(32):
            if flags & (1 << bit):
                vals[bit], = struct.unpack_from('<I', self.data, pos)
                pos += 4
        return vals

    def _read_item(self, off):
        d = self.data
        it = WsdItem()
        it.pan = 64
        it.pitch = 1.0
        it.events = []
        it.notes = []
        rid0, _, info_off = struct.unpack_from('<HHi', d, off)
        rid1, _, trk_off = struct.unpack_from('<HHi', d, off + 8)
        rid2, _, note_off = struct.unpack_from('<HHi', d, off + 16)
        if info_off != -1:
            vals = self._flag_values(off + info_off)
            pan_w = vals.get(0, 64)
            it.pan = pan_w & 0xFF
            if 1 in vals:
                it.pitch = struct.unpack('<f', struct.pack('<I', vals[1]))[0]
        # track info table -> note event tables
        if trk_off != -1:
            ttbl = off + trk_off
            tcount, = struct.unpack_from('<I', d, ttbl)
            for j in range(tcount):
                rid, _, roff = struct.unpack_from('<HHi', d, ttbl + 4 + j * 8)
                if roff == -1:
                    continue
                tinfo = ttbl + roff
                # track info: one ref to the note event table
                rid, _, evoff = struct.unpack_from('<HHi', d, tinfo)
                if evoff == -1:
                    continue
                etbl = tinfo + evoff
                ecount, = struct.unpack_from('<I', d, etbl)
                for k in range(ecount):
                    rid, _, eoff = struct.unpack_from('<HHi', d, etbl + 4 + k * 8)
                    if eoff == -1:
                        continue
                    e = etbl + eoff
                    pos_f, len_f, idx = struct.unpack_from('<ffI', d, e)
                    it.events.append((pos_f, len_f, idx))
        # note info table
        if note_off != -1:
            ntbl = off + note_off
            ncount, = struct.unpack_from('<I', d, ntbl)
            for j in range(ncount):
                rid, _, roff = struct.unpack_from('<HHi', d, ntbl + 4 + j * 8)
                if roff == -1:
                    it.notes.append(None)
                    continue
                noff = ntbl + roff
                n = WsdNote()
                n.wave_index, = struct.unpack_from('<I', d, noff)
                vals = self._flag_values(noff + 4)
                n.org_key = vals.get(0, 60) & 0xFF
                n.volume = vals.get(1, 127) & 0xFF
                n.pan = vals.get(2, 64) & 0xFF
                pitch_bits = vals.get(3, 0x3F800000)
                n.pitch = struct.unpack('<f', struct.pack('<I', pitch_bits))[0]
                n.attack = n.decay = n.sustain = n.hold = n.release = 127
                if 9 in vals and vals[9] != 0xFFFFFFFF:
                    env = noff + vals[9]
                    rid2, _, roff2 = struct.unpack_from('<HHi', d, env)
                    if roff2 != -1:
                        a = env + roff2
                        (n.attack, n.decay, n.sustain, n.hold, n.release) = d[a:a + 5]
                it.notes.append(n)
        return it
