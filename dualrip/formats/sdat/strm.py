"""STRM streamed-audio parsing and decode (the SDAT's streamed sound section)"""

import struct
import numpy as np
from .swar import decode_adpcm

STRM_MAGIC = b'STRM'
HEAD_MAGIC = b'HEAD'
DATA_MAGIC = b'DATA'

WAVE_PCM8 = 0
WAVE_PCM16 = 1
WAVE_ADPCM = 2

CODEC_NAMES = {WAVE_PCM8: 'PCM8', WAVE_PCM16: 'PCM16', WAVE_ADPCM: 'IMA-ADPCM'}

# HEAD at 0x10, its fields at 0x18; data always starts 0x68
HEAD_OFF = 0x10
FIELDS_OFF = 0x18
DATA_START = 0x68

# ADPCM block carries a 4-byte predictor preamble before its nibbles
ADPCM_PREAMBLE = 4


class Strm:
    """One STRM streamed sound: header fields plus its per-channel blocks."""

    __slots__ = (
        'wave_type', 'loop', 'channels', 'sample_rate', 'time', 'loop_start',
        'num_samples', 'num_blocks', 'block_size', 'block_samples',
        'last_block_size', 'last_block_samples', '_blocks',
    )

    def __init__(self, data):
        if data[:4] != STRM_MAGIC:
            raise ValueError('not a STRM file (magic %r)' % data[:4])
        if data[HEAD_OFF:HEAD_OFF + 4] != HEAD_MAGIC:
            raise ValueError('missing STRM HEAD block')
        head_size = struct.unpack_from('<I', data, HEAD_OFF + 4)[0]
        (
            self.wave_type, self.loop, self.channels, _unk,
            self.sample_rate, self.time, self.loop_start, self.num_samples,
            data_offset, self.num_blocks, self.block_size, self.block_samples,
            self.last_block_size, self.last_block_samples,
        ) = struct.unpack_from('<B?BB2H8I', data, FIELDS_OFF)
        if data_offset != DATA_START:
            raise ValueError('unexpected STRM data offset %#x' % data_offset)

        body = HEAD_OFF + head_size
        if data[body:body + 4] != DATA_MAGIC:
            raise ValueError('missing STRM DATA block')
        size = struct.unpack_from('<I', data, body + 4)[0]
        raw = data[body + 8:body + size]

        # holds the whole stream when the game stores it unsplit
        one_block = self.num_blocks == 1 and self.wave_type == WAVE_ADPCM
        self._blocks = [[] for _ in range(self.channels)]
        pos = 0
        for i in range(self.num_blocks):
            n = self.last_block_size if i == self.num_blocks - 1 else self.block_size
            if one_block:
                n += ADPCM_PREAMBLE
            for ch in range(self.channels):
                self._blocks[ch].append(raw[pos:pos + n])
                pos += n
            pos += -pos % 4

    @property
    def codec_name(self):
        return CODEC_NAMES.get(self.wave_type, hex(self.wave_type))

    def duration(self):
        return self.num_samples / self.sample_rate if self.sample_rate else 0.0

    def loop_points(self):
        """(start, end) loop points in samples, or None when the stream doesn't loop."""
        return (self.loop_start, self.num_samples) if self.loop else None

    def decode(self):
        """Decode to a (num_samples, channels) int16 array."""
        out = np.empty((self.num_samples, self.channels), dtype=np.int16)
        for ch, blocks in enumerate(self._blocks):
            out[:, ch] = _decode_channel(blocks, self.wave_type, self.num_samples)
        return out


def _decode_channel(blocks, wave_type, num_samples):
    """One channel's blocks to int16, trimmed to the authoritative sample count."""
    parts = []
    total = 0
    for blk in blocks:
        if total >= num_samples:
            break
        if wave_type == WAVE_PCM8:
            part = np.frombuffer(blk, dtype=np.int8).astype(np.int16) << 8
        elif wave_type == WAVE_PCM16:
            part = np.frombuffer(blk[:len(blk) & ~1], dtype='<i2')
        elif wave_type == WAVE_ADPCM:
            part = np.clip(decode_adpcm(bytes(blk)), -32768, 32767).astype(np.int16)
        else:
            raise NotImplementedError('STRM wave type %s not supported' % wave_type)
        parts.append(part)
        total += len(part)
    if not parts:
        return np.zeros(num_samples, dtype=np.int16)
    chan = np.concatenate(parts)
    if len(chan) < num_samples:
        chan = np.pad(chan, (0, num_samples - len(chan)))
    return chan[:num_samples]


def decode(data):
    """Decode STRM bytes to (sample_rate, (n, channels) int16 array, loop)."""
    strm = Strm(data)
    return strm.sample_rate, strm.decode(), strm.loop_points()
