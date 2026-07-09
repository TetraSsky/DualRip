# Part of DualRip. SWAR/WAV parsing + IMA-ADPCM decode. Ported from FeOS Sound
# System (fincs) / CyberBotX/in_xsf, derived from NNS driver disassembly.
# FIDELITY-CRITICAL: C integer semantics, table indexing are intentional.

import struct
import numpy as np
from .common import NNS_RECORD_COUNT_OFF, NNS_RECORD_TABLE_OFF

IMA_INDEX_TABLE = (-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8)
IMA_STEP_TABLE = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17,
    19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118,
    130, 143, 157, 173, 190, 209, 230, 253, 279, 307,
    337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
    876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066,
    2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358,
    5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
)

def decode_adpcm(raw):
    """
    Decode IMA-ADPCM block to int32 samples.

    Args:
        raw: bytes, 4-byte header (pred, index) + nibble-packed data.

    Returns:
        np.ndarray[int32] of decoded samples.
    """
    pred = struct.unpack_from('<h', raw, 0)[0]
    index = min(struct.unpack_from('<H', raw, 2)[0], 88)
    n = len(raw) - 4
    out = np.empty(n * 2, dtype=np.int32)
    step_tbl = IMA_STEP_TABLE
    idx_tbl = IMA_INDEX_TABLE
    pos = 0
    for byte in raw[4:]:
        for nibble in (byte & 0x0F, (byte >> 4) & 0x0F):
            step = step_tbl[index]
            index += idx_tbl[nibble]
            if index < 0:
                index = 0
            elif index > 88:
                index = 88
            diff = step >> 3
            if nibble & 4:
                diff += step
            if nibble & 2:
                diff += step >> 1
            if nibble & 1:
                diff += step >> 2
            if nibble & 8:
                pred -= diff
            else:
                pred += diff
            if pred < -0x8000:
                pred = -0x8000
            elif pred > 0x7FFF:
                pred = 0x7FFF
            out[pos] = pred
            pos += 1
    return out

class Swav:
    __slots__ = ('waveType', 'loop', 'sampleRate', 'time', 'loopStart', 'length', 'data')

    def __init__(self, data, off):
        self.waveType, self.loop, self.sampleRate, self.time, loopOffset, nonLoopLength = (
            struct.unpack_from('<BBHHHI', data, off)
        )
        size = (loopOffset + nonLoopLength) * 4
        raw = bytes(data[off + 12 : off + 12 + size])
        if self.waveType == 0: # PCM8
            self.data = np.frombuffer(raw, dtype=np.int8).astype(np.int32) << 8
            self.loopStart = loopOffset * 4
            self.length = nonLoopLength * 4
        elif self.waveType == 1: # PCM16
            self.data = np.frombuffer(raw[: 2 * (size // 2)], dtype='<i2').astype(np.int32)
            self.loopStart = loopOffset * 2
            self.length = nonLoopLength * 2
        else: # IMA-ADPCM
            self.data = decode_adpcm(raw)
            self.loopStart = (loopOffset - 1) * 8 if loopOffset else 0
            self.length = nonLoopLength * 8

    def __deepcopy__(self, memo):
        # SWAV data is immutable
        return self

def parse_swar(data):
    """Parse SWAR blob, return list of Swav."""
    count = struct.unpack_from('<I', data, NNS_RECORD_COUNT_OFF)[0]
    offs = struct.unpack_from('<%dI' % count, data, NNS_RECORD_TABLE_OFF)
    return [Swav(data, off) for off in offs]
