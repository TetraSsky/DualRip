"""SMDL sequence parsing (DSE): song/track chunks and the MIDI-like event bytecode."""

import struct

PAUSE_TICKS = [96, 72, 64, 48, 36, 32, 24, 18, 16, 12, 9, 8, 6, 4, 3, 2]

PARAM_LEN = {}
for _op in (0xAB, 0x95, 0x9C, 0xA9, 0xAA, 0xB1, 0xB2, 0xB3, 0xB5, 0xB6, 0xBC,
    0xBE, 0xBF, 0xC0, 0xC3, 0xD0, 0xD1, 0xD2, 0xDB, 0xDF, 0xE1, 0xE7,
    0xE9, 0xEF, 0xF6):
    PARAM_LEN[_op] = 1
for _op in (0xCB, 0xF8, 0xA8, 0xB4, 0xD5, 0xD6, 0xD8, 0xF2):
    PARAM_LEN[_op] = 2
for _op in (0xAF, 0xD4, 0xE2, 0xEA, 0xF3):
    PARAM_LEN[_op] = 3
for _op in (0xDD, 0xE5, 0xED, 0xF1):
    PARAM_LEN[_op] = 4
for _op in (0xDC, 0xE4, 0xEC, 0xF0):
    PARAM_LEN[_op] = 5

def parse_smdl(data):
    """(ticks_per_quarter_note, [per-track event bytes]) from a SMDL file."""
    if data[:4] != b'smdl':
        raise ValueError('not a SMDL')
    if data[0x40:0x44] != b'song':
        raise ValueError('missing song chunk')
    tpqn = struct.unpack_from('<H', data, 0x40 + 0x12)[0]
    nbtrks = data[0x40 + 0x16]
    tracks = []
    off = 0x40 + 0x40
    for _ in range(nbtrks):
        while off < len(data) and data[off:off + 4] != b'trk ':
            off += 1
        clen = struct.unpack_from('<I', data, off + 0x0C)[0]
        events = data[off + 0x10 + 4: off + 0x10 + clen] # skip trkid/chanid preamble
        tracks.append(events)
        off = off + 0x10 + clen
        off = (off + 3) & ~3
    return tpqn, tracks

def flatten_track(track_id, ev):
    """Walk one track's bytecode into timeline events (tick, kind, ...)."""
    out = []
    p = 0
    cur = 0
    octave = 0
    last_len = 0
    last_delay = 0
    n = len(ev)
    while p < n:
        b = ev[p]
        p += 1
        if b <= 0x7F:
            nd = ev[p]
            p += 1
            nbp = nd >> 6
            octave += ((nd >> 4) & 3) - 2
            note = nd & 0x0F
            if nbp:
                dur = 0
                for _k in range(nbp): # note duration is big-endian
                    dur = (dur << 8) | ev[p]
                    p += 1
                last_len = dur
            else:
                dur = last_len
            midi = 12 * octave + note
            out.append((cur, 'on', track_id, midi, b))
            # 0xFFFFFF is the "held note" sentinel (Common in SEDL SFX)
            if dur != 0xFFFFFF:
                out.append((cur + dur, 'off', track_id, midi))
        elif 0x80 <= b <= 0x8F:
            last_delay = PAUSE_TICKS[b - 0x80]
            cur += last_delay
        elif b == 0x90:
            cur += last_delay
        elif b == 0x91:
            last_delay += ev[p]
            p += 1
            cur += last_delay
        elif b == 0x92:
            last_delay = ev[p]
            p += 1
            cur += last_delay
        elif b == 0x93:
            last_delay = ev[p] | (ev[p + 1] << 8)
            p += 2
            cur += last_delay
        elif b == 0x94:
            last_delay = ev[p] | (ev[p + 1] << 8) | (ev[p + 2] << 16)
            p += 3
            cur += last_delay
        elif b == 0x98:
            break
        elif b == 0x99:
            pass
        elif b == 0xA0:
            octave = ev[p]
            p += 1
        elif b == 0xA1:
            octave += ev[p]
            p += 1
        elif b == 0xA4 or b == 0xA5:
            out.append((cur, 'tempo', ev[p]))
            p += 1
        elif b == 0xAC:
            out.append((cur, 'prog', track_id, ev[p]))
            p += 1
        elif b == 0xD7:
            v = ev[p] | (ev[p + 1] << 8)
            out.append((cur, 'bend', track_id, v - 65536 if v >= 32768 else v))
            p += 2
        elif b == 0xE0:
            out.append((cur, 'vol', track_id, ev[p]))
            p += 1
        elif b == 0xE3:
            out.append((cur, 'expr', track_id, ev[p]))
            p += 1
        elif b == 0xE8:
            out.append((cur, 'pan', track_id, ev[p]))
            p += 1
        else:
            p += PARAM_LEN.get(b, 0)
    return out
