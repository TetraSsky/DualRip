"""3DS CSEQ, CBNK, CWAR, CWAV parsing and CSAR-aware resolution."""

import struct
from . import cstm
from .csar import parse_cgrp

def s8(x):
    return ((x + 0x80) & 0xFF) - 0x80

class Cwav:
    """A decoded CWAV, one int16 list per channel."""

    __slots__ = ('rate', 'loop', 'loop_start', 'loop_end', 'samples')

    def __init__(self, data):
        if data[:4] != b'CWAV':
            raise ValueError('bad CWAV magic %r' % data[:4])
        nsec, = struct.unpack_from('<H', data, 0x10)
        secs = {}
        for i in range(nsec):
            sid, _, off, size = struct.unpack_from('<HHII', data, 0x14 + i * 0x0C)
            secs[sid] = off
        info = secs[0x7000]
        data_off = secs[0x7001]
        # body: codec u8, loop u8, pad u16, rate u32, loop_start u32, loop_end u32, reserved u32, then channel info reference table
        codec, loop = struct.unpack_from('<BB', data, info + 8)
        self.rate, self.loop_start, self.loop_end = struct.unpack_from(
            '<III', data, info + 0x0C)
        self.loop = bool(loop)
        tbl = info + 0x1C
        count, = struct.unpack_from('<I', data, tbl)
        chans = []
        for i in range(count):
            rid, _, roff = struct.unpack_from('<HHi', data, tbl + 4 + i * 8)
            ch = tbl + roff
            # channel info: samples ref (0x1F00, relative to DATA), codec-info ref (0x0300 DSP-ADPCM), reserved u32
            rid1, _, soff = struct.unpack_from('<HHi', data, ch)
            rid2, _, aoff = struct.unpack_from('<HHi', data, ch + 8)
            sample_off = data_off + 8 + soff
            n = self.loop_end
            if codec == 2: # DSP-ADPCM
                if rid2 != 0x0300:
                    raise ValueError('channel %d: codec info id %#06x' % (i, rid2))
                adp = ch + aoff
                coefs = struct.unpack_from('<16h', data, adp)
                _ps, h1, h2 = struct.unpack_from('<Hhh', data, adp + 0x20)
                raw = data[sample_off: sample_off + (n + 13) // 14 * 8 + 8]
                chans.append(cstm.decode_dsp_adpcm(raw, coefs, h1, h2, n))
            elif codec == 1: # PCM16
                chans.append(list(struct.unpack_from('<%dh' % n, data, sample_off)))
            elif codec == 0: # PCM8
                raw = data[sample_off: sample_off + n]
                chans.append([(b - 256 if b >= 128 else b) << 8 for b in raw])
            else:
                raise NotImplementedError('CWAV codec %d' % codec)
        # waves are mono in practice, mix down if stereo
        if len(chans) == 1:
            self.samples = chans[0]
        else:
            self.samples = [sum(c[i] for c in chans) // len(chans) for i in range(len(chans[0]))]

def parse_cwar(data):
    """CWAR to a list of raw CWAV blobs."""
    if data[:4] != b'CWAR':
        raise ValueError('bad CWAR magic %r' % data[:4])
    nsec, = struct.unpack_from('<H', data, 0x10)
    secs = {}
    for i in range(nsec):
        sid, _, off, size = struct.unpack_from('<HHII', data, 0x14 + i * 0x0C)
        secs[sid] = off
    info = secs[0x6800]
    file_off = secs[0x6801]
    tbl = info + 8
    count, = struct.unpack_from('<I', data, tbl)
    out = []
    for i in range(count):
        rid, _, off, size = struct.unpack_from('<HHIi', data, tbl + 4 + i * 0x0C)
        out.append(data[file_off + 8 + off: file_off + 8 + off + size])
    return out

REF_TBL_DIRECT = 0x6000
REF_TBL_INDEX = 0x6001
REF_TBL_RANGE = 0x6002

class VelRegion:
    """One velocity region of an instrument."""
    __slots__ = ('war_slot', 'wav_index', 'org_key', 'volume', 'pan', 'pitch', 'interp', 'attack', 'decay', 'sustain', 'hold', 'release')

class Cbnk:
    """CBNK instrument bank."""

    def __init__(self, data):
        if data[:4] != b'CBNK':
            raise ValueError('bad CBNK magic %r' % data[:4])
        self.data = data
        nsec, = struct.unpack_from('<H', data, 0x10)
        info = None
        for i in range(nsec):
            sid, _, off, size = struct.unpack_from('<HHII', data, 0x14 + i * 0x0C)
            if sid == 0x5800:
                info = off
        base = info + 8
        rid0, _, woff = struct.unpack_from('<HHi', data, base)
        rid1, _, ioff = struct.unpack_from('<HHi', data, base + 8)
        # wave id table
        self.waves = []
        if woff != -1:
            tbl = base + woff
            count, = struct.unpack_from('<I', data, tbl)
            for i in range(count):
                war_id, wav_idx = struct.unpack_from('<II', data, tbl + 4 + i * 8)
                self.waves.append((war_id & 0xFFFFFF, wav_idx))
        self.instruments = []
        if ioff != -1:
            tbl = base + ioff
            count, = struct.unpack_from('<I', data, tbl)
            for i in range(count):
                rid, _, roff = struct.unpack_from('<HHi', data, tbl + 4 + i * 8)
                self.instruments.append(None if roff == -1 else self._read_node(tbl + roff, 0))

    def _read_node(self, off, depth):
        """A node wraps one typed reference (direct/range/index table), entries lead to the next level, leaves are velocity regions."""
        d = self.data
        if depth == 2:
            return self._read_vel_region(off)
        rid, _, roff = struct.unpack_from('<HHi', d, off)
        if roff == -1:
            return None
        tbl = off + roff

        def child(coff):
            return None if coff is None else self._read_node(coff, depth + 1)

        if rid == REF_TBL_DIRECT:
            rid2, _, roff2 = struct.unpack_from('<HHi', d, tbl)
            return ('direct', child(tbl + roff2 if roff2 != -1 else None))
        if rid == REF_TBL_RANGE:
            start, end = struct.unpack_from('<BB', d, tbl)
            entries = []
            for j in range(end - start + 1):
                rid2, _, roff2 = struct.unpack_from('<HHi', d, tbl + 4 + j * 8)
                entries.append(child(tbl + roff2 if roff2 != -1 else None))
            return ('range', start, end, entries)
        if rid == REF_TBL_INDEX:
            n, = struct.unpack_from('<I', d, tbl)
            idx = list(d[tbl + 4: tbl + 4 + n])
            pos = tbl + 4 + ((n + 3) & ~3)
            entries = []
            for j in range(n):
                rid2, _, roff2 = struct.unpack_from('<HHi', d, pos + j * 8)
                entries.append(child(tbl + roff2 if roff2 != -1 else None))
            return ('index', idx, entries)
        raise ValueError('unknown region table id %#06x at %#x' % (rid, off))

    def _read_vel_region(self, off):
        d = self.data
        r = VelRegion()
        wave_index, flags = struct.unpack_from('<II', d, off)
        r.war_slot, r.wav_index = self.waves[wave_index]
        vals = {}
        pos = off + 8
        for bit in range(32):
            if flags & (1 << bit):
                vals[bit], = struct.unpack_from('<I', d, pos)
                pos += 4
        r.org_key = s8(vals.get(0, 60) & 0xFF)
        r.volume = vals.get(1, 127) & 0xFF
        pan_w = vals.get(2, 64)
        r.pan = s8(pan_w & 0xFF)
        pitch_bits = vals.get(3, 0x3F800000)
        r.pitch = struct.unpack('<f', struct.pack('<I', pitch_bits))[0]
        note_p = vals.get(4, 0)
        r.interp = (note_p >> 16) & 0xFF
        r.attack = r.decay = r.sustain = r.hold = r.release = 127
        if 9 in vals and vals[9] != 0xFFFFFFFF:
            env = off + vals[9]
            rid, _, roff = struct.unpack_from('<HHi', d, env)
            if roff != -1:
                a = env + roff
                r.attack, r.decay, r.sustain, r.hold, r.release = d[a:a + 5]
        return r

    @staticmethod
    def _select(node, value):
        if node is None:
            return None
        kind = node[0]
        if kind == 'direct':
            return node[1]
        if kind == 'range':
            _, start, end, entries = node
            if start <= value <= end:
                return entries[value - start]
            return None
        _, idx, entries = node
        for j, bound in enumerate(idx):
            if value <= bound:
                return entries[j]
        return None

    def lookup(self, program, key, velocity):
        if program >= len(self.instruments):
            return None
        inst = self.instruments[program]
        keyreg = self._select(inst, key)
        if keyreg is None:
            return None
        if isinstance(keyreg, VelRegion):
            return keyreg
        return self._select(keyreg, velocity)

def parse_cseq(data):
    """Returns (bytecode bytes, {label_name: offset})."""
    if data[:4] != b'CSEQ':
        raise ValueError('bad CSEQ magic %r' % data[:4])
    nsec, = struct.unpack_from('<H', data, 0x10)
    blob = None
    labels = {}
    for i in range(nsec):
        sid, _, off, size = struct.unpack_from('<HHII', data, 0x14 + i * 0x0C)
        if data[off:off + 4] == b'DATA':
            blob = data[off + 8: off + size]
        elif data[off:off + 4] == b'LABL':
            base = off + 8
            count, = struct.unpack_from('<I', data, base)
            for j in range(count):
                rid, _, roff = struct.unpack_from('<HHi', data, base + 4 + j * 8)
                e = base + roff
                rid2, _, doff = struct.unpack_from('<HHi', data, e)
                slen, = struct.unpack_from('<I', data, e + 8)
                labels[data[e + 12: e + 12 + slen].decode('ascii')] = doff
    return blob, labels

class SoundContext:
    """Resolves sounds/banks/wars through the CSAR (group-aware)."""

    def __init__(self, csar, group_variants, prefer_group=None):
        self.csar = csar
        self.group_variants = group_variants # fid -> {bytes: [labels]}
        self.prefer_group = prefer_group
        self._cwar_cache = {}
        self._cwav_cache = {}
        self._bank_cache = {}

    def file_bytes(self, fid, what):
        blob = self.csar.file_data(fid)
        if blob:
            return blob
        variants = self.group_variants.get(fid)
        if not variants:
            raise LookupError('file %d (%s) not found anywhere' % (fid, what))
        if len(variants) == 1:
            return next(iter(variants))
        if self.prefer_group:
            for fdata, labels in variants.items():
                if any(self.prefer_group in lb for lb in labels):
                    return fdata
        groups = sorted({lb for v in variants.values() for lb in v})
        raise LookupError(
            'file %d (%s) exists in %d per-group variants; pass --group with '
            'one of: %s' % (fid, what, len(variants), ', '.join(groups)))

    def bank(self, bank_item):
        if bank_item not in self._bank_cache:
            fid, _name = self.csar.banks[bank_item]
            self._bank_cache[bank_item] = Cbnk(self.file_bytes(fid, 'bank %d' % bank_item))
        return self._bank_cache[bank_item]

    def cwav(self, war_item, wav_index):
        key = (war_item, wav_index)
        if key not in self._cwav_cache:
            if war_item not in self._cwar_cache:
                fid = self.csar.wars[war_item]
                self._cwar_cache[war_item] = parse_cwar(
                    self.file_bytes(fid, 'war %d' % war_item))
            self._cwav_cache[key] = Cwav(self._cwar_cache[war_item][wav_index])
        return self._cwav_cache[key]

    def make_lookup(self, bank_items):
        def lookup(bank_no, program, key, vel):
            if bank_no >= len(bank_items) or bank_items[bank_no] == 0xFFFFFF:
                return None
            bank = self.bank(bank_items[bank_no])
            region = bank.lookup(program, key, vel)
            if region is None:
                return None
            return region, self.cwav(region.war_slot, region.wav_index)
        return lookup

def build_group_variants(csar, ext_groups):
    group_variants = {}
    all_groups = []
    for gfid, gname in csar.groups:
        blob = csar.file_data(gfid)
        if blob:
            all_groups.append((gname or 'group_%d' % gfid, blob))
    all_groups += ext_groups
    for label, blob in all_groups:
        for fid, fdata in parse_cgrp(blob).items():
            group_variants.setdefault(fid, {}).setdefault(fdata, []).append(label)
    return group_variants
