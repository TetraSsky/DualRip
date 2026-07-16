"""3DS CSAR sound archive: STRG strings, INFO tables, FILE data."""

import struct

# Section ids (CSAR header)
SEC_STRG = 0x2000
SEC_INFO = 0x2001
SEC_FILE = 0x2002

# INFO table reference
REF_SOUNDS = 0x2100
REF_SOUND_GROUPS = 0x2104
REF_BANKS = 0x2101
REF_WARS = 0x2103
REF_GROUPS = 0x2105
REF_PLAYERS = 0x2102
REF_FILES = 0x2106

# Item detail
DETAIL_STREAM = 0x2201
DETAIL_WAVE = 0x2202
DETAIL_SEQ = 0x2203

FILE_INTERNAL = 0x220C
FILE_EXTERNAL = 0x220D

DETAIL_NAMES = {DETAIL_STREAM: 'stream', DETAIL_WAVE: 'wave', DETAIL_SEQ: 'sequence'}

# CGRP section
CGRP_INFO = 0x7800
CGRP_FILE = 0x7801

def read_ref(data, off):
    """(id, target_offset) offset is relative to `off` unless negative."""
    rid, _, roff = struct.unpack_from('<HHi', data, off)
    return rid, roff

def read_sized_ref(data, off):
    rid, _, roff, size = struct.unpack_from('<HHIi', data, off)
    return rid, roff, size

def nw4c_sections(data, magic):
    if data[:4] != magic:
        raise ValueError('bad magic %r (expected %r)' % (data[:4], magic))
    num, = struct.unpack_from('<H', data, 0x10)
    secs = {}
    for i in range(num):
        sid, _, off, size = struct.unpack_from('<HHII', data, 0x14 + i * 0x0C)
        secs[sid] = (off, size)
    return secs

class CtrSoundEntry:
    """One named sound in the archive."""
    __slots__ = ('index', 'name', 'file_id', 'player_id', 'volume', 'kind', 'bank_ids', 'alloc_track_flags', 'start_offset', 'channel_prio', 'wsd_index')

    def __init__(self):
        self.name = ''
        self.bank_ids = []
        self.alloc_track_flags = None
        self.start_offset = 0
        self.channel_prio = None
        self.wsd_index = 0

class Csar:
    """Parsed CSAR archive."""

    def __init__(self, data):
        self.data = data
        secs = nw4c_sections(data, b'CSAR')
        self.strg_off, _ = secs[SEC_STRG]
        self.info_off, _ = secs[SEC_INFO]
        self.file_off, _ = secs[SEC_FILE]
        self._parse_strings()
        self._parse_info()
        self._parse_files()

    def _parse_strings(self):
        base = self.strg_off + 8
        _, tbl_off = read_ref(self.data, base) # table ref
        tbl = base + tbl_off
        count, = struct.unpack_from('<I', self.data, tbl)
        self.names = []
        for i in range(count):
            _, off, size = read_sized_ref(self.data, tbl + 4 + i * 0x0C)
            self.names.append(self.data[tbl + off: tbl + off + size - 1].decode('ascii', 'replace'))

    def name(self, string_id):
        if string_id is None:
            return ''
        idx = string_id & 0xFFFFFF
        return self.names[idx] if idx < len(self.names) else ''

    def _info_table(self, ref_id):
        base = self.info_off + 8
        for i in range(8):
            rid, roff = read_ref(self.data, base + i * 8)
            if rid == ref_id:
                tbl = base + roff
                count, = struct.unpack_from('<I', self.data, tbl)
                out = []
                for j in range(count):
                    rid2, roff2 = read_ref(self.data, tbl + 4 + j * 8)
                    out.append(tbl + roff2 if roff2 != -1 else None)
                return out
        raise ValueError('INFO table %#06x not found' % ref_id)

    @staticmethod
    def _flag_values(data, off, flags):
        """One u32 per set flag bit, in ascending bit order."""
        vals = {}
        pos = off
        for bit in range(32):
            if flags & (1 << bit):
                vals[bit], = struct.unpack_from('<I', data, pos)
                pos += 4
        return vals

    def _parse_info(self):
        d = self.data
        self.sounds = []
        for off in self._info_table(REF_SOUNDS):
            s = CtrSoundEntry()
            s.index = len(self.sounds)
            if off is not None:
                s.file_id, s.player_id, s.volume = struct.unpack_from('<IIB', d, off)
                did, doff = read_ref(d, off + 0x0C)
                s.kind = DETAIL_NAMES.get(did, hex(did))
                flags, = struct.unpack_from('<I', d, off + 0x14)
                vals = self._flag_values(d, off + 0x18, flags)
                s.name = self.name(vals.get(0))
                if did == DETAIL_SEQ:
                    det = off + doff
                    _, btoff = read_ref(d, det) # bank id table
                    s.alloc_track_flags, seq_flags = struct.unpack_from('<II', d, det + 8)
                    # flag values: bit0 = start offset into the CSEQ, bit1 = channel priority (default 64)
                    seq_vals = self._flag_values(d, det + 0x10, seq_flags)
                    s.start_offset = seq_vals.get(0, 0)
                    s.channel_prio = seq_vals.get(1, 64) & 0xFF
                    btbl = det + btoff
                    cnt, = struct.unpack_from('<I', d, btbl)
                    s.bank_ids = [struct.unpack_from('<I', d, btbl + 4 + k * 4)[0] & 0xFFFFFF for k in range(cnt)]
                elif did == DETAIL_WAVE:
                    # CWSD file (items are 0-indexed sequentially)
                    s.wsd_index, = struct.unpack_from('<I', d, off + doff)
            self.sounds.append(s)

        # Banks: fileId u32, war-table ref, flags, optional values (bit0=name)
        self.banks = []
        for off in self._info_table(REF_BANKS):
            file_id, = struct.unpack_from('<I', d, off)
            flags, = struct.unpack_from('<I', d, off + 0x0C)
            vals = self._flag_values(d, off + 0x10, flags)
            self.banks.append((file_id, self.name(vals.get(0))))

        # Wave archives: fileId u32, u32 (flags/type), no name observed
        self.wars = []
        for off in self._info_table(REF_WARS):
            file_id, = struct.unpack_from('<I', d, off)
            self.wars.append(file_id)

        # sound groups: contiguous sound-index ranges
        self.sound_groups = []
        for off in self._info_table(REF_SOUND_GROUPS):
            first, last = struct.unpack_from('<II', d, off)
            flags, = struct.unpack_from('<I', d, off + 0x18)
            vals = self._flag_values(d, off + 0x1C, flags)
            self.sound_groups.append((first & 0xFFFFFF, last & 0xFFFFFF, self.name(vals.get(0))))

        # Groups: fileId u32, flags, optional name
        self.groups = []
        for off in self._info_table(REF_GROUPS):
            file_id, = struct.unpack_from('<I', d, off)
            flags, = struct.unpack_from('<I', d, off + 4)
            vals = self._flag_values(d, off + 8, flags)
            self.groups.append((file_id, self.name(vals.get(0))))

    def _parse_files(self):
        d = self.data
        self.files = [] # per fileId: ('internal', off, size) | ('external', path) | ('null',)
        for off in self._info_table(REF_FILES):
            rid, roff = read_ref(d, off)
            if rid == FILE_INTERNAL:
                loc = off + roff
                rid2, foff, fsize = read_sized_ref(d, loc)
                # ref 0x1F00 = plain file, 0x0000 = embedded CGRP, offset 0xFFFFFFFF = absent from FILE
                if foff != 0xFFFFFFFF:
                    self.files.append(('internal', self.file_off + 8 + foff, fsize))
                else:
                    self.files.append(('null',))
            elif rid == FILE_EXTERNAL:
                raw = d[off + roff:]
                self.files.append(('external', raw[:raw.index(0)].decode('ascii')))
            else:
                self.files.append(('null',))

    def file_data(self, file_id, group_files=None):
        """Resolve a fileId to bytes, using the CGRP group map when needed."""
        kind = self.files[file_id]
        if kind[0] == 'internal':
            _, off, size = kind
            return self.data[off:off + size]
        if group_files and file_id in group_files:
            return group_files[file_id]
        return None

def parse_cgrp(data):
    """CGRP container -> {fileId: bytes}."""
    secs = nw4c_sections(data, b'CGRP')
    info_off, _ = secs[CGRP_INFO]
    file_off, _ = secs[CGRP_FILE]
    base = info_off + 8
    count, = struct.unpack_from('<I', data, base)
    out = {}
    for i in range(count):
        _, roff = read_ref(data, base + 4 + i * 8)
        entry = base + roff
        file_id, = struct.unpack_from('<I', data, entry)
        _, foff, fsize = read_sized_ref(data, entry + 4)
        if foff == 0xFFFFFFFF:
            continue
        out[file_id] = data[file_off + 8 + foff: file_off + 8 + foff + fsize]
    return out

def safe(name):
    return ''.join(c if c.isalnum() or c in '_-' else '_' for c in name)
