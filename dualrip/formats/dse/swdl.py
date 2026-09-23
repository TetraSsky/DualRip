"""SWDL bank parsing (DSE): wavi samples, prgi programs, pcmd data, ADPCM decode."""

import struct
import numpy as np

_STEP = np.array([
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41,
    45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190,
    209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724,
    796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272,
    2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132,
    7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350,
    22385, 24623, 27086, 29794, 32767], dtype=np.int32)

_INDEX = np.array([-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8], dtype=np.int32)

def decode_adpcm(data):
    """IMA-ADPCM bytes to int16 array (4-byte preamble: predictor u16, step index u16)."""
    nibbles = np.empty((len(data) - 4) * 2, dtype=np.int32)
    body = np.frombuffer(data[4:], dtype=np.uint8).astype(np.int32)
    nibbles[0::2] = body & 0x0F
    nibbles[1::2] = body >> 4
    out = np.empty(len(nibbles) + 1, dtype=np.int16)
    new_sample = data[0] | (data[1] << 8)
    if new_sample >= 0x8000:
        new_sample -= 0x10000
    index = (data[2] | (data[3] << 8)) & 0x7F
    out[0] = new_sample
    step_tbl = _STEP
    idx_tbl = _INDEX
    ns = new_sample
    for i in range(len(nibbles)):
        d = int(nibbles[i])
        step = int(step_tbl[index])
        diff = step >> 3
        if d & 4:
            diff += step
        if d & 2:
            diff += step >> 1
        if d & 1:
            diff += step >> 2
        if d & 8:
            diff = -diff
        ns += diff
        if ns > 32767:
            ns = 32767
        elif ns < -32768:
            ns = -32768
        out[i + 1] = ns
        index += int(idx_tbl[d])
        if index < 0:
            index = 0
        elif index > 88:
            index = 88
    return out

class Sample:
    """One wavi sample: format, loop, rate, and raw or decoded data."""
    __slots__ = ('id', 'fmt', 'root_key', 'fine_tune', 'coarse_tune', 'volume', 'pan', 'loop_enabled', 'sample_rate', 'loop_beg', 'loop_len', 'raw', 'pcm16')

    def pcm(self):
        """Decoded int16 samples (cached)."""
        if self.pcm16 is None:
            if self.fmt == 0x200:
                self.pcm16 = decode_adpcm(self.raw)
            elif self.fmt == 0x100:
                self.pcm16 = np.frombuffer(self.raw, dtype='<i2')
            else:
                self.pcm16 = np.zeros(0, dtype=np.int16)
        return self.pcm16

class Split:
    """One prgi split: a key/velocity range mapped to a sample with an envelope."""
    __slots__ = ('low_key', 'high_key', 'low_vel', 'high_vel', 'sample_id', 'fine_tune', 'coarse_tune', 'root_key', 'volume', 'pan', 'env_on', 'env_mult', 'atk_vol', 'attack', 'decay', 'sustain', 'hold', 'decay2', 'release')

class Program:
    """One prgi program: volume, pan, and a list of splits."""
    __slots__ = ('id', 'volume', 'pan', 'splits')

class Swdl:
    """Parsed SWDL bank: samples by id, programs by id, and the sample-bank flag."""

    def __init__(self, data):
        if data[:4] != b'swdl':
            raise ValueError('not a SWDL (%r)' % data[:4])
        self.is_sample_bank = data[0x0E] != 0
        self.pcmdlen = struct.unpack_from('<I', data, 0x40)[0]
        self.nb_wavi = struct.unpack_from('<H', data, 0x46)[0]
        self.nb_prgi = struct.unpack_from('<H', data, 0x48)[0]
        self.samples = {}
        self.programs = {}
        chunks = self._chunks(data)
        pcmd = chunks.get('pcmd')
        pcmd_data = data[pcmd[0]:pcmd[0] + pcmd[1]] if pcmd else None
        if 'wavi' in chunks:
            self._parse_wavi(data, chunks['wavi'], pcmd_data)
        if 'prgi' in chunks:
            self._parse_prgi(data, chunks['prgi'])

    def _chunks(self, data):
        chunks = {}
        off = 0x50
        while off < len(data) - 16:
            label = data[off:off + 4]
            if label in (b'wavi', b'prgi', b'kgrp', b'pcmd', b'eod '):
                clen = struct.unpack_from('<I', data, off + 0x0C)[0]
                chunks[label.decode().strip()] = (off + 0x10, clen)
                if label == b'eod ':
                    break
                off = (off + 0x10 + clen + 15) & ~15
            else:
                off += 4
        return chunks

    def _parse_wavi(self, data, chunk, pcmd_data):
        base, _clen = chunk
        ptrs = struct.unpack_from('<%dH' % self.nb_wavi, data, base)
        for p in ptrs:
            if p == 0:
                continue
            e = base + p
            s = Sample()
            s.id = struct.unpack_from('<H', data, e + 0x02)[0]
            s.fine_tune, s.coarse_tune, s.root_key = struct.unpack_from('<3b', data, e + 0x04)
            s.volume, s.pan = struct.unpack_from('<2b', data, e + 0x08)
            s.fmt = struct.unpack_from('<H', data, e + 0x12)[0]
            s.loop_enabled = data[e + 0x15] != 0
            s.sample_rate = struct.unpack_from('<I', data, e + 0x20)[0]
            pos, loopbeg, looplen = struct.unpack_from('<3I', data, e + 0x24)
            s.raw = None
            s.pcm16 = None
            if s.fmt == 0x200:
                s.loop_beg = (loopbeg - 1) * 8
                s.loop_len = looplen * 8
            elif s.fmt == 0x100:
                s.loop_beg = loopbeg * 2
                s.loop_len = looplen * 2
            else:
                s.loop_beg = loopbeg * 4
                s.loop_len = looplen * 4
            if pcmd_data is not None:
                nbytes = (loopbeg + looplen) * 4
                s.raw = pcmd_data[pos:pos + nbytes]
            self.samples[s.id] = s

    def _parse_prgi(self, data, chunk):
        base, _clen = chunk
        ptrs = struct.unpack_from('<%dH' % self.nb_prgi, data, base)
        for p in ptrs:
            if p == 0:
                continue
            e = base + p
            prog = Program()
            prog.id, nsplits = struct.unpack_from('<2H', data, e)
            prog.volume, prog.pan = struct.unpack_from('<2b', data, e + 0x04)
            nlfo = data[e + 0x0B]
            prog.splits = []
            so = e + 0x10 + nlfo * 0x10 + 0x10
            for k in range(nsplits):
                base_s = so + k * 0x30
                sp = Split()
                sp.sample_id = struct.unpack_from('<H', data, base_s + 0x12)[0]
                sp.low_key, sp.high_key = struct.unpack_from('<2b', data, base_s + 0x04)
                sp.low_vel, sp.high_vel = struct.unpack_from('<2b', data, base_s + 0x08)
                sp.fine_tune, sp.coarse_tune, sp.root_key = struct.unpack_from('<3b', data, base_s + 0x14)
                sp.volume, sp.pan = struct.unpack_from('<2b', data, base_s + 0x18)
                sp.env_on = data[base_s + 0x20] != 0
                sp.env_mult = data[base_s + 0x21]
                (sp.atk_vol, sp.attack, sp.decay, sp.sustain, sp.hold, sp.decay2, sp.release) = struct.unpack_from('<7b', data, base_s + 0x28)
                prog.splits.append(sp)
            self.programs[prog.id] = prog
