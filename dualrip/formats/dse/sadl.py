"""SADL streamed-audio decoder (Procyon-ADPCM DS streams)."""

import struct
import numpy as np

SADL_MAGIC = b'sadl'
BLOCK = 0x10

# standard XA/PSX coefficients << 6
PROC_COEFS = [(0, 0), (60, 0), (115, -52), (98, -55), (122, -60)] + [(0, 0)] * 11

def _cdiv(a, b):
    """C integer division: truncate toward zero (b > 0)."""
    return a // b if a >= 0 else -((-a) // b)

def _clamp16(v):
    return -32768 if v < -32768 else (32767 if v > 32767 else v)

def _s4lo(byte):
    v = byte & 0x0F
    return (v & 7) - (v & 8)

def _s4hi(byte):
    v = (byte >> 4) & 0x0F
    return (v & 7) - (v & 8)

class SadlStream:
    """Parsed SADL header."""

    def __init__(self, data):
        if data[:4] != SADL_MAGIC:
            raise ValueError('not a SADL file (magic %r)' % data[:4])
        self.loop_flag = data[0x31]
        self.channels = data[0x32]
        flags = data[0x33]
        self.codec = flags & 0xF0
        self.sample_rate = 32728 if (flags & 0x06) == 0x04 else 16364
        self.data_size = struct.unpack_from('<I', data, 0x40)[0]
        self.start_offset = struct.unpack_from('<I', data, 0x48)[0]
        self.loop_start_byte = struct.unpack_from('<I', data, 0x54)[0]
        self.body_size = self.data_size - self.start_offset
        self.loop_start_byte -= self.start_offset

    def frames_per_channel(self):
        return self.body_size // self.channels // BLOCK

    def num_samples(self):
        return self.frames_per_channel() * 30

    def loop(self):
        """(start, end) loop points in per-channel samples, or None."""
        if not self.loop_flag:
            return None
        start = self.loop_start_byte // self.channels // BLOCK * 30
        return (start, self.num_samples())

    @property
    def codec_name(self):
        return {0xB0: 'Procyon', 0x70: 'IMA', 0x00: 'IMA'}.get(self.codec, hex(self.codec))

    def describe(self):
        loop = 'loop %d..%d' % self.loop() if self.loop() else 'no loop'
        n = self.num_samples()
        return '%s, %d ch, %d Hz, %d samples (%.2fs), %s' % (self.codec_name, self.channels, self.sample_rate, n, n / self.sample_rate, loop)

def _decode_procyon_channel(body, chan, channels):
    """One channel's 0x10-byte Procyon frames (block-deinterleaved) to int16 list."""
    nframes = len(body) // BLOCK
    out = []
    hist1 = hist2 = 0
    for b in range(chan, nframes, channels):
        frame = body[b * BLOCK: b * BLOCK + BLOCK]
        header = frame[0x0F] ^ 0x80
        scale = 12 - (header & 0xF)
        index = (header >> 4) & 0xF
        coef1, coef2 = PROC_COEFS[index]
        for i in range(30):
            nib = frame[i // 2] ^ 0x80
            s = _s4hi(nib) if (i & 1) else _s4lo(nib)
            s = s << 12
            s = (s << -scale) if scale < 0 else (s >> scale)
            s = _cdiv(hist1 * coef1 + hist2 * coef2 + 32, 64) + s * 64
            hist2 = hist1
            hist1 = s
            out.append(_cdiv(_clamp16(_cdiv(s + 32, 64)), 64) * 64)
    return out


def decode(data):
    """
    Decode SADL bytes to (sample_rate, stereo int16 ndarray (n,2), loop).

    Mono streams are duplicated to both channels so the shared stereo WAV writer and player handle them unchanged."""
    info = SadlStream(data)
    if info.codec != 0xB0:
        raise NotImplementedError('SADL codec %s not supported (only Procyon 0xb0)' % hex(info.codec))
    body = data[info.start_offset: info.start_offset + info.body_size]
    chans = [np.asarray(_decode_procyon_channel(body, c, info.channels), dtype=np.int16) for c in range(info.channels)]
    if len(chans) == 1:
        left = right = chans[0]
    else:
        left, right = chans[0], chans[1]
    n = min(len(left), len(right))
    stereo = np.empty((n, 2), dtype=np.int16)
    stereo[:, 0] = left[:n]
    stereo[:, 1] = right[:n]
    return info.sample_rate, stereo, info.loop()
