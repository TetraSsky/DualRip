# Part of DualRip. Core playback logic is a faithful Python port of the FeOS
# Sound System (fincs), as adapted by Naram Qashat (CyberBotX) for the NCSF
# player (github.com/CyberBotX/in_xsf, src/in_ncsf/SSEQPlayer). Lookup tables
# come from disassembly of Nintendo's NNS sound driver by those authors.
# FIDELITY-CRITICAL: C integer semantics (truncating division, arithmetic
# shifts, table indexing) are intentional. Do not "simplify".

from collections import Counter
from .engine.sequencer import (
    EXTRA_BYTE,
    SSEQ_CMD_CALL,
    SSEQ_CMD_FIN,
    SSEQ_CMD_FROM_VAR,
    SSEQ_CMD_GOTO,
    SSEQ_CMD_OPEN_TRACK,
    SSEQ_CMD_PATCH,
    SSEQ_CMD_RANDOM,
    SSEQ_NOTE_LIMIT,
    SSEQ_VAR_CMD_FIRST,
    SSEQ_VAR_CMD_LAST,
    VARIABLE_BYTE_COUNT,
    readvl,
    sseq_command_byte_count,
)

MAX_SCAN_STEPS = 2000 # per-branch safety bound for malformed bytecode

def scan_patches(blob, off):
    """Static scan of the patches (instrument numbers) a sequence entry uses."""
    out = set()
    todo = [off]
    seen = set()
    while todo:
        pc = todo.pop()
        for _ in range(MAX_SCAN_STEPS):
            if pc in seen or pc < 0 or pc >= len(blob):
                break
            seen.add(pc)
            cmd = blob[pc]
            pc += 1
            if cmd == SSEQ_CMD_FIN:
                break
            elif cmd == SSEQ_CMD_PATCH:
                v, pc = readvl(blob, pc)
                out.add(v)
            elif cmd < SSEQ_NOTE_LIMIT:  # note-on: velocity + varlen length
                pc += 1
                _v, pc = readvl(blob, pc)
            elif cmd == SSEQ_CMD_OPEN_TRACK:
                todo.append(blob[pc + 1] | (blob[pc + 2] << 8) | (blob[pc + 3] << 16))
                pc += 4
            elif cmd in (SSEQ_CMD_GOTO, SSEQ_CMD_CALL):
                todo.append(blob[pc] | (blob[pc + 1] << 8) | (blob[pc + 2] << 16))
                pc += 3
                if cmd == SSEQ_CMD_GOTO:
                    break
            elif cmd in (SSEQ_CMD_RANDOM, SSEQ_CMD_FROM_VAR):
                sub = blob[pc]
                pc += 1
                if (SSEQ_VAR_CMD_FIRST <= sub <= SSEQ_VAR_CMD_LAST) or sub < SSEQ_NOTE_LIMIT:
                    pc += 1
                pc += 4 if cmd == SSEQ_CMD_RANDOM else 1
            else:
                nb = sseq_command_byte_count(cmd)
                pc += nb & ~(VARIABLE_BYTE_COUNT | EXTRA_BYTE)
                if nb & VARIABLE_BYTE_COUNT:
                    _v, pc = readvl(blob, pc)
    # a sequence with no PATCH command plays with the default patch 0
    return out or {0}

def patch_playable(entries, slot_sizes, p):
    """True if patch p exists and all its instruments can actually resolve
    their sample (wave archive slot present and wave index in range).
    slot_sizes: number of waves in each of the bank's 4 wave archive slots."""
    if p >= len(entries) or not entries[p].record:
        return False
    for inst in entries[p].instruments:
        if inst.record == 1:
            if inst.swar >= len(slot_sizes) or inst.swav >= slot_sizes[inst.swar]:
                return False
    return True

def parse_bank_map(text):
    """Parse "4=32,30=6" or "4=32+33+43" into {src: [candidates]}."""
    out = {}
    if text:
        for pair in text.split(','):
            src, dst = pair.split('=')
            out[int(src)] = [int(x) for x in dst.split('+')]
    return out

class BankResolver:
    """Resolve NULL/dynamic bank slots via family-affinity + coverage ranking."""

    def __init__(self, sdat, seqarc, override_map=None):
        self.sdat = sdat
        self.seqarc = seqarc
        self.override = dict(override_map or {})
        self.auto_bids = set()
        self.auto_candidates = []
        self._entry_ps = {}
        self._prepare()

    def _prepare(self):
        valid = [e for e in self.seqarc.entries if e.offset is not None]
        self.auto_bids = {
            e.bank_id
            for e in valid
            if e.bank_id not in self.override and self.sdat.bank_is_null(e.bank_id)
        }
        if not self.auto_bids:
            return
        blob = self.seqarc.blob
        self._entry_ps = {
            e.index: scan_patches(blob, e.offset) for e in valid if e.bank_id in self.auto_bids
        }
        # who can fully play each entry?
        coverers = {i: [] for i in self._entry_ps}
        for bid in range(self.sdat.num_banks):
            meta = self.sdat.bank_meta(bid)
            if meta is None:
                continue
            ent, cnts, _w = meta
            for i, ps in self._entry_ps.items():
                if all(patch_playable(ent, cnts, p) for p in ps):
                    coverers[i].append(bid)
        # exclusivity-weighted coverage: an entry only one bank can play
        # weighs 1, an entry every bank can play weighs almost nothing
        scores = {}
        for i, bids in coverers.items():
            if not bids:
                continue
            w = 1.0 / len(bids)
            for b in bids:
                scores[b] = scores.get(b, 0.0) + w
        # family affinity: each (slot, archive) pair carries the coverage
        # mass of the banks sharing it, so the family that actually plays
        # this archive dominates (e.g. a shared/base bank plus per-level
        # or per-object banks that all reuse the same slot layout)
        pair_mass = Counter()
        for b, sc in scores.items():
            for s, wid in enumerate(self.sdat.bank_meta(b)[2]):
                if wid is not None:
                    pair_mass[(s, wid)] += sc

        def affinity(bid):
            return sum(
                pair_mass[(s, wid)]
                for s, wid in enumerate(self.sdat.bank_meta(bid)[2])
                if wid is not None
            )

        self.auto_candidates = sorted(scores, key=lambda b: (-affinity(b), -scores[b], b))

    @property
    def note(self):
        if not self.auto_bids:
            return None
        return (
            f'bank slot(s) {sorted(self.auto_bids)} are NULL in the SDAT '
            f'(filled at runtime by the game); auto-resolving each entry '
            f'across {len(self.auto_candidates)} real banks'
        )

    def coverage(self, entry, bid):
        """Fraction of the entry's instruments playable with bank `bid`."""
        ps = self._entry_ps.get(entry.index)
        if ps is None:
            ps = scan_patches(self.seqarc.blob, entry.offset)
        meta = self.sdat.bank_meta(bid)
        if meta is None or not ps:
            return 0.0
        ent, cnts, _w = meta
        return sum(1 for p in ps if patch_playable(ent, cnts, p)) / len(ps)

    def resolve(self, entry):
        """Bank id to use for this entry."""
        bid = entry.bank_id
        cands = self.override.get(bid)
        if not cands and bid in self.auto_bids:
            cands = self.auto_candidates
        if not cands:
            return bid
        if len(cands) == 1:
            return cands[0]
        ps = self._entry_ps.get(entry.index)
        if ps is None:
            ps = scan_patches(self.seqarc.blob, entry.offset)
        best, best_cov = cands[0], -1
        for c in cands:
            meta = self.sdat.bank_meta(c)
            if meta is None:
                continue
            ent, cnts, _w = meta
            cov = sum(1 for p in ps if patch_playable(ent, cnts, p))
            if ps and cov == len(ps):
                return c
            if cov > best_cov:
                best, best_cov = c, cov
        return best
