"""
DSE facade: exposes a ROM's embedded DSE audio as renderable sounds.

Three renderable kinds share one archive, mirroring the CTR archive's unified sound list:
- 'seq' (SMDL sequenced music)
- 'sfx' (SEDL sequenced effects)
- 'stream' (SADL streamed audio). Sequenced kinds resolve against a preset bank + sample bank and go through the DSE synth; streams are decoded directly.
"""

import os
from collections import OrderedDict
from . import sadl as sadl_mod
from . import sedl as sedl_mod
from . import smdl as smdl_mod
from .gfsa import extract_dse_files
from .swdl import Swdl

KIND_LABEL = {'seq': 'Music sequence (SMDL)', 'sfx': 'Sound effect (SEDL)', 'stream': 'Stream (SADL)'}

class DseEntry:
    """One renderable DSE sound. Sequenced kinds carry events/preset/bank"""
    __slots__ = ('index', 'name', 'kind', 'tpqn', 'ntrk', 'events', 'preset', 'bank', 'bank_name', 'data', 'info')

def _display_name(member_name):
    """Human name of a DSE member: its internal name, minus the numeric container prefix"""
    stem = os.path.splitext(os.path.basename(member_name))[0]
    parts = stem.split('_', 2)
    return parts[2] if len(parts) == 3 and parts[0].isdigit() else stem

def _flatten(tracks):
    """Sorted timeline events from a list of per-track event byte strings."""
    events = []
    for tid, ev in enumerate(tracks):
        events.extend(smdl_mod.flatten_track(tid, ev))
    events.sort(key=lambda e: e[0])
    return events

def _programs_used(events):
    return {e[3] for e in events if e[1] == 'prog'}

def _sample_ids(preset):
    return {sp.sample_id for pr in preset.programs.values() for sp in pr.splits}

class DseArchive:
    """Parsed DSE audio (sequences, effects, streams) resolved against banks."""

    def __init__(self, files, label='DSE'):
        """files: iterable of (key, display name, bytes) for the DSE members of one ROM."""
        self.label = label
        self.display = {}
        swds, smds, sedls, sadls = {}, {}, {}, {}
        for key, disp, data in files:
            self.display[key] = disp
            magic = data[:4]
            if magic == b'swdl':
                swds[key] = Swdl(data)
            elif magic == b'smdl':
                smds[key] = data
            elif magic == b'sedl':
                sedls[key] = data
            elif magic == sadl_mod.SADL_MAGIC:
                sadls[key] = data
        self.sample_banks = {n: s for n, s in swds.items() if s.is_sample_bank}
        self.presets = {n: s for n, s in swds.items() if not s.is_sample_bank}

        self.sounds = []
        self.unresolved = []
        self._idx = 0
        seqs, sfx, streams = [], [], []
        for name, data in sorted(smds.items()):
            e = self._build_seq(name, data)
            (seqs if e else self.unresolved).append(e or name)
        for name, data in sorted(sedls.items()):
            got = self._build_sfx(name, data)
            (sfx if got else self.unresolved).extend(got or [name])
        for name, data in sorted(sadls.items()):
            e = self._build_stream(name, data)
            (streams if e else self.unresolved).append(e or name)
        self.sounds = seqs + sfx + streams

        self.folders = OrderedDict()
        if seqs:
            self.folders['SEQ'] = sorted(seqs, key=lambda s: s.name)
        if sfx:
            self.folders['SFX'] = sorted(sfx, key=lambda s: s.name)
        if streams:
            self.folders['STRM'] = sorted(streams, key=lambda s: s.name)
        self._by_index = {s.index: s for s in self.sounds}

    def _next(self):
        i = self._idx
        self._idx += 1
        return i

    def _build_seq(self, name, data):
        """A sequenced-music entry, or None if its banks don't resolve."""
        tpqn, tracks = smdl_mod.parse_smdl(data)
        events = _flatten(tracks)
        preset = self._pair_preset(name, events)
        bank_name, bank = self._pair_bank(preset) if preset else (None, None)
        if preset is None or bank is None:
            return None
        e = DseEntry()
        e.index, e.name, e.kind = self._next(), self.display.get(name, name), 'seq'
        e.tpqn, e.ntrk, e.events = tpqn, len(tracks), events
        e.preset, e.bank, e.bank_name = preset, bank, bank_name
        e.data = e.info = None
        return e

    def _build_sfx(self, name, data):
        """One entry per SFX sequence in a SEDL file, or None if unresolved."""
        seqs = sedl_mod.parse_sedl(data)
        if not seqs:
            return None
        used = set()
        for _tpqn, tracks in seqs:
            used |= _programs_used(_flatten(tracks))
        preset = self._pair_preset(name, [(0, 'prog', 0, p) for p in used])
        bank_name, bank = self._pair_bank(preset) if preset else (None, None)
        if preset is None or bank is None:
            return None
        base = self.display.get(name, name)
        out = []
        multi = len(seqs) > 1
        for i, (tpqn, tracks) in enumerate(seqs):
            e = DseEntry()
            e.index, e.kind = self._next(), 'sfx'
            e.name = f'{base}_{i:02d}' if multi else base
            e.tpqn, e.ntrk, e.events = tpqn, len(tracks), _flatten(tracks)
            e.preset, e.bank, e.bank_name = preset, bank, bank_name
            e.data = e.info = None
            out.append(e)
        return out

    def _build_stream(self, name, data):
        """A streamed-audio entry, or None if the header won't parse."""
        try:
            info = sadl_mod.SadlStream(data)
        except ValueError:
            return None
        e = DseEntry()
        e.index, e.name, e.kind = self._next(), self.display.get(name, name), 'stream'
        e.tpqn = e.ntrk = e.events = e.preset = e.bank = None
        e.bank_name = None
        e.data, e.info = data, info
        return e

    def _pair_preset(self, seq_name, events):
        """The preset bank paired with a sequence, matched by name then program set."""
        stem = os.path.splitext(os.path.basename(seq_name))[0]
        disp = _display_name(seq_name)
        key = stem.split('_', 2)[0]
        for pname, preset in self.presets.items():
            pstem = os.path.splitext(os.path.basename(pname))[0]
            if _display_name(pname) == disp or pstem.split('_', 2)[0] == key or pstem == stem:
                return preset
        used = _programs_used(events)
        for _pname, preset in self.presets.items():
            if used and used <= set(preset.programs):
                return preset
        return None

    def _pair_bank(self, preset):
        """The sample bank that satisfies a preset's program splits, or None if none do."""
        need = _sample_ids(preset)
        full = [n for n, b in self.sample_banks.items() if need <= set(b.samples)]
        if full:
            return self.display.get(full[0], full[0]), self.sample_banks[full[0]]
        if not need:
            n = next(iter(self.sample_banks), None)
            return (self.display.get(n, n), self.sample_banks[n]) if n else (None, None)
        cands = [n for n, b in self.sample_banks.items() if need & set(b.samples)]
        if not cands:
            return None, None
        best = max(cands, key=lambda n: (len(need & set(self.sample_banks[n].samples)), len(self.sample_banks[n].samples)))
        return self.display.get(best, best), self.sample_banks[best]

    def sound(self, index):
        return self._by_index[index]

    def counts(self):
        by = {'seq': 0, 'sfx': 0, 'stream': 0}
        for s in self.sounds:
            by[s.kind] += 1
        return by

    def render(self, entry, rate):
        """Render a DSE sound to stereo int16 samples at the given rate."""
        if entry.kind == 'stream':
            native_rate, stereo, loop = sadl_mod.decode(entry.data)
            return stereo, native_rate, loop
        from ...engine.dse.synth import render
        audio = render(entry.events, entry.tpqn, entry.ntrk, entry.preset, entry.bank, rate, drain_held=(entry.kind == 'sfx'))
        return audio, rate, None

def _rom_file_names(rom):
    """idx -> path for every file in a .nds filesystem."""
    names = {}

    def walk(folder, prefix=''):
        for f in folder.files:
            path = (prefix + '/' + f).lstrip('/')
            names[rom.filenames.idOf(path)] = path
        for name, sub in folder.folders:
            walk(sub, prefix + '/' + name)

    walk(rom.filenames)
    return names

def _member_key(names, idx):
    """Unique member key for a loose file, from its whole ROM path."""
    path = names.get(idx)
    return os.path.splitext(path)[0].replace('/', '_') if path else 'member_%04d' % idx

def _member_name(names, idx):
    """ROM file name of a loose member, without its extension."""
    return os.path.splitext(os.path.basename(names.get(idx, 'member_%04d' % idx)))[0]

def dse_files_in_rom(nds_path):
    """Yield (key, display name, bytes) for every DSE member in a .nds ROM's filesystem."""
    import ndspy.rom
    rom = ndspy.rom.NintendoDSRom.fromFile(nds_path)
    names = _rom_file_names(rom)
    files = []
    for idx, data in enumerate(rom.files):
        magic = bytes(data[:4])
        if magic == b'GFSA':
            for key, blob in extract_dse_files(bytes(data)):
                files.append((key, _display_name(key), blob))
        elif magic in (b'swdl', b'smdl', sadl_mod.SADL_MAGIC):
            files.append((_member_key(names, idx), _member_name(names, idx), bytes(data)))
    return files

def open_dse_rom(nds_path, label=None):
    """A DseArchive built directly from a .nds ROM's embedded DSE audio."""
    files = dse_files_in_rom(nds_path)
    return DseArchive(files, label or os.path.basename(nds_path))
