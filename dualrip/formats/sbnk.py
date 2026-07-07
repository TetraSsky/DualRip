# Part of DualRip. Core playback logic is a faithful Python port of the FeOS
# Sound System (fincs), as adapted by Naram Qashat (CyberBotX) for the NCSF
# player (github.com/CyberBotX/in_xsf, src/in_ncsf/SSEQPlayer). Lookup tables
# come from disassembly of Nintendo's NNS sound driver by those authors.
# FIDELITY-CRITICAL: C integer semantics (truncating division, arithmetic
# shifts, table indexing) are intentional. Do not "simplify".

import struct
from .common import NNS_RECORD_COUNT_OFF, NNS_RECORD_TABLE_OFF

class NoteDef:
    __slots__ = (
        'lowNote',
        'highNote',
        'record',
        'swav',
        'swar',
        'noteNumber',
        'attackRate',
        'decayRate',
        'sustainLevel',
        'releaseRate',
        'pan',
    )

    def __init__(self, low, high, record, data=None, off=None):
        self.lowNote, self.highNote, self.record = low, high, record
        if data is not None:
            (
                self.swav,
                self.swar,
                self.noteNumber,
                self.attackRate,
                self.decayRate,
                self.sustainLevel,
                self.releaseRate,
                self.pan,
            ) = struct.unpack_from('<HH6B', data, off)

class BankEntry:
    __slots__ = ('record', 'instruments')

def parse_sbnk(data):
    count = struct.unpack_from('<I', data, NNS_RECORD_COUNT_OFF)[0]
    entries = []
    pos = NNS_RECORD_TABLE_OFF
    for _ in range(count):
        record = data[pos]
        offset = struct.unpack_from('<H', data, pos + 1)[0]
        pos += 4
        e = BankEntry()
        e.record = record
        e.instruments = []
        if record:
            if record == 16:
                low, high = data[offset], data[offset + 1]
                p = offset + 2
                for i in range(high - low + 1):
                    rec = struct.unpack_from('<H', data, p)[0]
                    e.instruments.append(NoteDef(low + i, low + i, rec, data, p + 2))
                    p += 12
            elif record == 17:
                ranges = data[offset : offset + 8]
                p = offset + 8
                i = 0
                while i < 8 and ranges[i]:
                    rec = struct.unpack_from('<H', data, p)[0]
                    low = ranges[i - 1] + 1 if i else 0
                    e.instruments.append(NoteDef(low, ranges[i], rec, data, p + 2))
                    p += 12
                    i += 1
            else:
                e.instruments.append(NoteDef(0, 127, record, data, offset))
        entries.append(e)
    return entries
