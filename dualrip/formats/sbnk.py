# Part of DualRip. SBNK instrument bank parsing. Ported from FeOS Sound System
# (fincs) / CyberBotX/in_xsf, derived from NNS driver disassembly.
# FIDELITY-CRITICAL: C integer semantics are intentional.

import struct
from .common import NNS_RECORD_COUNT_OFF, NNS_RECORD_TABLE_OFF

class NoteDef:
    """Single note > sample mapping with ADSR envelope and pan."""

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
    """One bank slot: record type + list of NoteDef."""
    
    __slots__ = ('record', 'instruments')

def parse_sbnk(data):
    """
    Parse SBNK blob into list of BankEntry.

    Record type 16 = key-range, 17 = multi-range, other = single instrument.
    """
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
