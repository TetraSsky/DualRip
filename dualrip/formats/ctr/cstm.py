"""3DS BCSTM stream decode to PCM (PCM8, PCM16, DSP-ADPCM, IMA-ADPCM)."""

import struct

CSTM_MAGIC = b'CSTM'
BOM_LE = 0xFEFF

SECTION_INFO = 0x4000
SECTION_SEEK = 0x4001
SECTION_DATA = 0x4002

REF_STREAM_INFO = 0x4100
REF_TRACK_INFO = 0x4101
REF_CHANNEL_INFO = 0x4102
REF_DSP_ADPCM_INFO = 0x0300
REF_IMA_ADPCM_INFO = 0x0301
REF_SAMPLE_DATA = 0x1F00
REF_TABLE = 0x0100

CODEC_PCM8 = 0
CODEC_PCM16 = 1
CODEC_DSP_ADPCM = 2
CODEC_IMA_ADPCM = 3

CODEC_NAMES = {0: 'PCM8', 1: 'PCM16', 2: 'DSP-ADPCM', 3: 'IMA-ADPCM'}

class StreamInfo:
    """Decoded INFO block of a CSTM file."""

    def __init__(self, data):
        if data[:4] != CSTM_MAGIC:
            raise ValueError('not a CSTM file (magic %r)' % data[:4])
        bom, = struct.unpack_from('<H', data, 0x04)
        if bom != BOM_LE:
            raise ValueError('unexpected byte order mark %#06x' % bom)
        self.file_size, = struct.unpack_from('<I', data, 0x0C)
        num_sections, = struct.unpack_from('<H', data, 0x10)

        sections = {}
        for i in range(num_sections):
            sid, _, off, size = struct.unpack_from('<HHII', data, 0x14 + i * 0x0C)
            sections[sid] = (off, size)
        if SECTION_INFO not in sections or SECTION_DATA not in sections:
            raise ValueError('missing INFO or DATA section')
        self.sections = sections

        info_off, _ = sections[SECTION_INFO]
        if data[info_off:info_off + 4] != b'INFO':
            raise ValueError('INFO magic not found')
        base = info_off + 0x08 # reference offsets are relative to here

        # three refs in fixed order (stream, track table, channel table), read positionally since both tables share id 0x0101
        refs = []
        for i in range(3):
            rid, _, roff = struct.unpack_from('<HHi', data, base + i * 0x08)
            refs.append(roff)
        stream_ref, _track_ref, channel_ref = refs
        si = base + stream_ref

        (self.codec, self.loop_flag, self.num_channels, _pad,
         self.sample_rate, self.loop_start, self.sample_count,
         self.num_blocks, self.block_size, self.block_samples,
         self.last_block_size, self.last_block_samples, self.last_block_padded,
         self.seek_size, self.seek_interval) = struct.unpack_from('<4B11I', data, si)

        rid, _, roff = struct.unpack_from('<HHi', data, si + 0x30)
        if rid != REF_SAMPLE_DATA:
            raise ValueError('unexpected sample data reference id %#06x' % rid)
        data_off, _ = sections[SECTION_DATA]
        self.sample_data_off = data_off + 0x08 + roff

        # Channel info table -> per-channel codec parameters
        self.channels = []
        if channel_ref >= 0:
            tbl = base + channel_ref
            count, = struct.unpack_from('<I', data, tbl)
            for i in range(count):
                rid, _, roff = struct.unpack_from('<HHi', data, tbl + 4 + i * 0x08)
                ch = tbl + roff
                rid2, _, roff2 = struct.unpack_from('<HHi', data, ch)
                if self.codec == CODEC_DSP_ADPCM:
                    if rid2 != REF_DSP_ADPCM_INFO:
                        raise ValueError('channel %d: expected DSP-ADPCM info, got %#06x' % (i, rid2))
                    self.channels.append(DspChannelInfo(data, ch + roff2))
                else:
                    self.channels.append(None)
        if self.codec == CODEC_DSP_ADPCM and len(self.channels) != self.num_channels:
            raise ValueError('channel info count %d != channel count %d' % (len(self.channels), self.num_channels))

    def describe(self):
        loop = ('loop %d..%d' % (self.loop_start, self.sample_count)
        if self.loop_flag else 'no loop')
        return ('%s, %d ch, %d Hz, %d samples (%.2fs), %s' % (CODEC_NAMES.get(self.codec, self.codec), self.num_channels, self.sample_rate, self.sample_count, self.sample_count / self.sample_rate, loop))


class DspChannelInfo:
    """DSP-ADPCM parameters for one channel (16 coefs + initial context)."""

    def __init__(self, data, off):
        self.coefs = struct.unpack_from('<16h', data, off)
        (self.pred_scale, self.hist1, self.hist2, self.loop_pred_scale, self.loop_hist1, self.loop_hist2, _pad) = struct.unpack_from('<H2hH2hH', data, off + 0x20)


def decode_dsp_adpcm(raw, coefs, hist1, hist2, num_samples):
    """Decode DSP-ADPCM bytes to a list of int16 samples."""
    out = [0] * num_samples
    pos = 0
    n = 0
    while n < num_samples:
        # frame: 1 header byte (scale low nibble, coef index high) + 7 data bytes = 14 samples, high nibble first
        header = raw[pos]
        pos += 1
        scale = 1 << (header & 0x0F)
        ci = (header >> 4) * 2
        c1 = coefs[ci]
        c2 = coefs[ci + 1]
        frame_samples = min(14, num_samples - n)
        take = (frame_samples + 1) // 2
        for b in raw[pos:pos + take]:
            for nib in ((b >> 4), (b & 0x0F)):
                if n >= num_samples:
                    break
                if nib >= 8:
                    nib -= 16
                s = (nib * scale * 2048 + 1024 + c1 * hist1 + c2 * hist2) >> 11
                if s > 32767:
                    s = 32767
                elif s < -32768:
                    s = -32768
                out[n] = s
                n += 1
                hist2 = hist1
                hist1 = s
        pos += take
    return out


def decode_cstm(data):
    """Decode a CSTM file to (StreamInfo, per-channel int16 lists)."""
    info = StreamInfo(data)
    nch = info.num_channels
    chans = [[] for _ in range(nch)]

    if info.codec == CODEC_DSP_ADPCM:
        hist = [(c.hist1, c.hist2) for c in info.channels]
    elif info.codec not in (CODEC_PCM8, CODEC_PCM16):
        raise NotImplementedError('codec %s not supported yet' % CODEC_NAMES.get(info.codec, info.codec))

    pos = info.sample_data_off
    remaining = info.sample_count
    for blk in range(info.num_blocks):
        last = blk == info.num_blocks - 1
        bsize = info.last_block_size if last else info.block_size
        bpad = info.last_block_padded if last else info.block_size
        bsamples = min(info.block_samples, remaining)
        for ch in range(nch):
            raw = data[pos:pos + bsize]
            if info.codec == CODEC_DSP_ADPCM:
                h1, h2 = hist[ch]
                samples = decode_dsp_adpcm(raw, info.channels[ch].coefs, h1, h2, bsamples)
                if samples:
                    hist[ch] = (samples[-1], samples[-2] if len(samples) > 1 else h1)
                chans[ch].extend(samples)
            elif info.codec == CODEC_PCM16:
                chans[ch].extend(struct.unpack('<%dh' % bsamples, raw[:bsamples * 2]))
            else: # PCM8 (signed)
                chans[ch].extend((b - 256 if b >= 128 else b) << 8 for b in raw[:bsamples])
            pos += bpad
        remaining -= bsamples
    return info, chans


def write_wav(path, chans, rate, loop=None):
    """Write per-channel int16 lists to a RIFF WAV, loop into a smpl chunk."""
    nch = len(chans)
    nsamp = len(chans[0])
    frames = bytearray(nsamp * nch * 2)
    # interleave channel columns byte by byte
    packed = [struct.pack('<%dh' % nsamp, *ch) for ch in chans]
    for ci in range(nch):
        frames[ci * 2::nch * 2] = packed[ci][0::2]
        frames[ci * 2 + 1::nch * 2] = packed[ci][1::2]

    chunks = []
    fmt = struct.pack('<HHIIHH', 1, nch, rate, rate * nch * 2, nch * 2, 16)
    chunks.append(b'fmt ' + struct.pack('<I', len(fmt)) + fmt)
    if loop is not None:
        start, end = loop
        smpl = struct.pack('<9I', 0, 0, 1000000000 // rate, 60, 0, 0, 0, 1, 0)
        smpl += struct.pack('<6I', 0, 0, start, end, 0, 0)
        chunks.append(b'smpl' + struct.pack('<I', len(smpl)) + smpl)
    chunks.append(b'data' + struct.pack('<I', len(frames)) + bytes(frames))
    body = b'WAVE' + b''.join(chunks)
    with open(path, 'wb') as f:
        f.write(b'RIFF' + struct.pack('<I', len(body)) + body)
