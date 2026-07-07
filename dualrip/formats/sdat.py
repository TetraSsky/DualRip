"""SDAT container access for DualRip.

This is the ONLY module that touches ndspy. Everything else works on plain
Python/numpy structures, so the container backend can be swapped or
internalized later without touching the engine.
"""

import struct

import ndspy.soundArchive

from .common import (
    BANK_WAVE_ARCHIVE_SLOTS,
    NNS_RECORD_COUNT_OFF,
    NO_WAVE_ARCHIVE,
)
from .sbnk import parse_sbnk
from .swar import parse_swar


def _swar_wave_count(war):
    """Number of waves in an ndspy wave archive, without decoding samples."""
    raw = bytes(war.save()[0][: NNS_RECORD_COUNT_OFF + 4])
    return struct.unpack_from('<I', raw, NNS_RECORD_COUNT_OFF)[0]


class SeqArcEntry:
    """One sound entry of a sequence archive (SSAR)."""

    __slots__ = ('index', 'name', 'bank_id', 'volume', 'cpr', 'offset')

    def __init__(self, index, name, bank_id, volume, cpr, offset):
        self.index = index
        self.name = name
        self.bank_id = bank_id
        self.volume = volume
        self.cpr = cpr  # channel-priority base the game passes at runtime
        self.offset = offset  # None for null/placeholder slots


class SeqArc:
    """A sequence archive: shared event blob + entry table."""

    __slots__ = ('arc_id', 'name', 'blob', 'entries')

    def __init__(self, arc_id, name, blob, entries):
        self.arc_id = arc_id
        self.name = name
        self.blob = blob
        self.entries = entries


class SdatFile:
    """Read-only view over a sound_data.sdat with lazy, cached parsing."""

    def __init__(self, path):
        self.path = path
        self._sdat = ndspy.soundArchive.SDAT.fromFile(path)
        self._seqarc_cache = {}
        self._bank_cache = {}
        self._meta_cache = {}
        self._swar_cache = {}

    @property
    def seqarc_list(self):
        """[(arc_id, name, entry_count)] for every non-null archive."""
        out = []
        for i, (name, arc) in enumerate(self._sdat.sequenceArchives):
            if arc is not None:
                out.append((i, name or f'SEQARC_{i}', len(arc.sequences)))
        return out

    def seqarc(self, arc_id):
        if arc_id not in self._seqarc_cache:
            name, arc = self._sdat.sequenceArchives[arc_id]
            if arc is None:
                raise ValueError(f'sequence archive {arc_id} is null')
            entries = []
            for idx, (sname, seq) in enumerate(arc.sequences):
                if seq is None or seq.firstEventOffset is None:
                    entries.append(SeqArcEntry(idx, sname or f'SEQ_{idx}',
                                               None, None, None, None))
                else:
                    entries.append(
                        SeqArcEntry(
                            idx,
                            sname or f'SEQ_{idx}',
                            seq.bankID,
                            seq.volume,
                            seq.channelPressure,  # ndspy's name for cpr
                            seq.firstEventOffset,
                        )
                    )
            self._seqarc_cache[arc_id] = SeqArc(
                arc_id, name or f'SEQARC_{arc_id}', bytes(arc.eventsData), entries
            )
        return self._seqarc_cache[arc_id]

    @property
    def sequence_list(self):
        """[(seq_id, name, bank_id)] for every non-null SSEQ (music)."""
        out = []
        for i, (name, seq) in enumerate(self._sdat.sequences):
            if seq is not None:
                out.append((i, name or f'SSEQ_{i}', getattr(seq, 'bankID', None)))
        return out

    @property
    def bank_list(self):
        """[(bank_id, name_or_None, wave_archive_ids_or_None)]; None ids for
        NULL/dynamic slots."""
        out = []
        for i, (name, bnk) in enumerate(self._sdat.banks):
            if bnk is None:
                out.append((i, name, None))
            else:
                out.append((i, name or f'BANK_{i}',
                            list(bnk.waveArchiveIDs)[:BANK_WAVE_ARCHIVE_SLOTS]))
        return out

    @property
    def wave_archive_list(self):
        """[(war_id, name, wave_count)] for every non-null wave archive."""
        out = []
        for i, (name, war) in enumerate(self._sdat.waveArchives):
            if war is not None:
                out.append((i, name or f'SWAR_{i}', _swar_wave_count(war)))
        return out

    @property
    def num_banks(self):
        return len(self._sdat.banks)

    def bank_name(self, bid):
        if 0 <= bid < len(self._sdat.banks):
            return self._sdat.banks[bid][0]
        return None

    def bank_is_null(self, bid):
        return not 0 <= bid < len(self._sdat.banks) or self._sdat.banks[bid][1] is None

    def _bank_slot_ids(self, bnk):
        """The bank's wave archive ids, normalized to exactly
        BANK_WAVE_ARCHIVE_SLOTS entries with None for empty/invalid slots."""
        wids = []
        for wid in list(bnk.waveArchiveIDs)[:BANK_WAVE_ARCHIVE_SLOTS]:
            if (
                wid is None
                or wid == NO_WAVE_ARCHIVE
                or self._sdat.waveArchives[wid][1] is None
            ):
                wids.append(None)
            else:
                wids.append(wid)
        while len(wids) < BANK_WAVE_ARCHIVE_SLOTS:
            wids.append(None)
        return wids

    def bank_meta(self, bid):
        """(patch entries, wave counts per slot, wave archive ids) without
        decoding any sample, or None for a null bank."""
        if bid not in self._meta_cache:
            if self.bank_is_null(bid):
                self._meta_cache[bid] = None
            else:
                bnk = self._sdat.banks[bid][1]
                entries = parse_sbnk(bytes(bnk.save()[0]))
                wids = self._bank_slot_ids(bnk)
                counts = [
                    0 if w is None
                    else _swar_wave_count(self._sdat.waveArchives[w][1])
                    for w in wids
                ]
                self._meta_cache[bid] = (entries, counts, wids)
        return self._meta_cache[bid]

    def _swar(self, wid):
        if wid not in self._swar_cache:
            war = self._sdat.waveArchives[wid][1]
            self._swar_cache[wid] = parse_swar(bytes(war.save()[0]))
        return self._swar_cache[wid]

    def bank(self, bid):
        """(patch entries, [4 decoded wave archives or None]) for rendering."""
        if bid not in self._bank_cache:
            if self.bank_is_null(bid):
                raise ValueError(
                    f'bank {bid} is a NULL/dynamic slot filled at runtime by '
                    f'the game; substitute a real bank (bank map)'
                )
            bnk = self._sdat.banks[bid][1]
            entries = parse_sbnk(bytes(bnk.save()[0]))
            wave_arc = [
                None if wid is None else self._swar(wid)
                for wid in self._bank_slot_ids(bnk)
            ]
            self._bank_cache[bid] = (entries, wave_arc)
        return self._bank_cache[bid]
