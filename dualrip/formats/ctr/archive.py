"""3DS CSAR facade: exposes a ROM or loose .bcsar as sound folders."""

from __future__ import annotations
import os
from collections import OrderedDict
from . import cseq, cstm, cwsd
from .csar import Csar, safe
from .rom import open_romfs, romfs_walk
from ...engine.ctr import render_entry, render_wsd

# CSAR detail kind -> short label
KIND_LABEL = {'sequence': 'seq', 'wave': 'wsd', 'stream': 'strm'}
KIND_TITLE = {
    'seq': 'Sequence sound (CSEQ)',
    'wsd': 'Wave sound (CWSD)',
    'strm': 'Stream (BCSTM)',
}

def find_csars_in_rom(rom_path, boot9=None):
    """One CtrArchive per .bcsar in a ROM, boot9 required for encrypted images."""
    reader, romfs = open_romfs(rom_path, boot9=boot9)
    csar_paths = [p for p in romfs_walk(romfs) if p.lower().endswith('.bcsar')]
    if not csar_paths:
        raise ValueError(f'No .bcsar sound archive found in {rom_path!r}')
    ext_groups = []
    for p in romfs_walk(romfs):
        if p.lower().endswith('.bcgrp'):
            with romfs.open(p) as f:
                ext_groups.append((os.path.splitext(os.path.basename(p))[0], f.read()))
    rom_name = os.path.basename(rom_path)
    archives = []
    for p in csar_paths:
        with romfs.open(p) as f:
            data = f.read()
        label = rom_name if len(csar_paths) == 1 else f'{rom_name} [{os.path.basename(p)}]'
        archives.append(CtrArchive(
            data, ext_groups, label,
            romfs=romfs, reader=reader, csar_dir=os.path.dirname(p),
        ))
    return archives

def open_bcsar(path):
    """Open a loose .bcsar, reading .bcgrp groups from a sibling extData/ dir."""
    with open(path, 'rb') as f:
        data = f.read()
    csar_dir = os.path.dirname(os.path.abspath(path))
    ext_groups = []
    extdata = os.path.join(csar_dir, 'extData')
    if os.path.isdir(extdata):
        for nm in sorted(os.listdir(extdata)):
            if nm.lower().endswith('.bcgrp'):
                with open(os.path.join(extdata, nm), 'rb') as f:
                    ext_groups.append((os.path.splitext(nm)[0], f.read()))
    return CtrArchive(data, ext_groups, os.path.basename(path), csar_dir=csar_dir)

class CtrArchive:
    """Parsed CSAR with group variants and a render context."""

    def __init__(self, csar_data, ext_groups, label, romfs=None, reader=None, csar_dir=None):
        self.label = label
        self._romfs = romfs
        self._reader = reader # keeps the reader's file handles alive
        self._csar_dir = csar_dir or ''
        self.csar = Csar(csar_data)
        self.group_variants = cseq.build_group_variants(self.csar, ext_groups)
        self.ctx = cseq.SoundContext(self.csar, self.group_variants)
        self.unapplied = {} # parsed-but-unapplied command counts

        group_of = {}
        for gi, (first, last, gname) in enumerate(self.csar.sound_groups):
            for idx in range(first, last + 1):
                group_of[idx] = (gi, gname or 'group_%d' % gi)
        self.sounds = [s for s in self.csar.sounds if s.name]
        grouped = {}
        streams = []
        ungrouped = []
        for s in self.sounds:
            if KIND_LABEL[s.kind] == 'strm':
                streams.append(s)
            elif s.index in group_of:
                gi, gname = group_of[s.index]
                grouped.setdefault((gi, gname), []).append(s)
            else:
                ungrouped.append(s)
        self.folders = OrderedDict()
        for (gi, gname), members in sorted(grouped.items()):
            self.folders['%03d_%s' % (gi, safe(gname))] = sorted(members, key=lambda s: s.name)
        if streams:
            self.folders['STRM'] = sorted(streams, key=lambda s: s.name)
        if ungrouped:
            self.folders['_ungrouped'] = sorted(ungrouped, key=lambda s: s.name)
        self._by_index = {s.index: s for s in self.sounds}

    def sound(self, index):
        return self._by_index[index]

    def kind_label(self, s):
        return KIND_LABEL[s.kind]

    def counts(self):
        c = {'seq': 0, 'wsd': 0, 'strm': 0}
        for s in self.sounds:
            c[KIND_LABEL[s.kind]] += 1
        return c

    def bank_wave_archives(self, bank_index):
        """Sorted unique wave-archive indices used by a bank's wave table."""
        bank = self.ctx.bank(bank_index)
        return sorted({war for war, _wav_idx in bank.waves})

    def war_wave_count(self, war_index):
        """Wave count of one CWAR without decoding samples."""
        fid = self.csar.wars[war_index]
        blob = self.ctx.file_bytes(fid, 'war %d' % war_index)
        return len(cseq.parse_cwar(blob))

    def _read_stream_file(self, rel):
        """Read an external stream file (RomFS path relative to the CSAR)"""
        path = (self._csar_dir + '/' + rel) if self._csar_dir else rel
        if self._romfs is not None:
            with self._romfs.open(path) as f:
                return f.read()
        with open(os.path.join(self._csar_dir, rel.replace('/', os.sep)), 'rb') as f:
            return f.read()

    def render(self, s, rate):
        """Render one sound to (native_rate, chans, loop)."""
        kind = KIND_LABEL[s.kind]
        if kind == 'strm':
            entry = self.csar.files[s.file_id]
            if entry[0] != 'external':
                raise LookupError('stream file %d not external' % s.file_id)
            info, chans = cstm.decode_cstm(self._read_stream_file(entry[1]))
            loop = (info.loop_start, info.sample_count) if info.loop_flag else None
            return info.sample_rate, chans, loop
        if kind == 'seq':
            blob, _labels = cseq.parse_cseq(self.ctx.file_bytes(s.file_id, 'seq'))
            chans, loop, unapplied = render_entry(
                blob, s.start_offset, self.ctx.make_lookup(s.bank_ids), rate,
                s.channel_prio or 64, base_vol=s.volume)
            for k, v in unapplied.items():
                self.unapplied[k] = self.unapplied.get(k, 0) + v
            return rate, chans, loop
        item = cwsd.Cwsd(self.ctx.file_bytes(s.file_id, 'wsd'))
        chans = render_wsd(self.ctx, item, s.wsd_index, rate, s.volume)
        return rate, chans, None
