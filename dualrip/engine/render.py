# Part of DualRip. Render entry points (streaming + batch) + duration estimator.
# Faithful port of FeOS Sound System (fincs) / CyberBotX/in_xsf, derived from
# NNS driver disassembly.
# FIDELITY-CRITICAL: C integer semantics, clock/emit arithmetic are intentional.

import copy
import numpy as np
from ..cprims import (
    CS_ATTACK,
    CS_NONE,
    CS_RELEASE,
    CS_START,
    CS_SUSTAIN,
    SECONDS_PER_CLOCK,
    TS_END,
    cnv_scale,
)
from .sequencer import Player

STREAM_CHUNK_SAMPLES = 8820

class LiveRenderer:
    """
    Resumable, seekable driver for one SSAR/SSEQ entry.

    Owns a Player + clock/emit bookkeeping. step(produce=True) > int16 block, step(produce=False) > silent fast-forward (bit-exact state advance).
    snapshot()/from_snapshot() enable checkpoint+ffwd seek in live preview.

    Raw policy: one loop iteration, full release envelopes, native rests, PSG/noise endless notes released hold_seconds after idle, ~15ms cold-start warm-up skipped. player_prio = cpr from SDAT INFO.
    """

    def __init__(self, blob, start_offset, bank, waveArc, entry_volume, rate, hold_seconds=2.0, player_prio=0):
        self.rate = rate
        self.hold_seconds = hold_seconds
        self.sps = 1.0 / rate
        self.ply = Player(blob, bank, waveArc, rate, cnv_scale(entry_volume), player_prio)
        self.ply.setup(start_offset)
        self.ply.timer()
        self.seconds_into = 0.0
        self.next_clock = SECONDS_PER_CLOCK
        self.total = 0
        self.emitted = 0
        self.recording = False
        self.tick0_ran = False
        self.ended_at = None
        self.finished = False

    @staticmethod
    def _share_memo(ply):
        return {
            id(ply.blob): ply.blob,
            id(ply.bank): ply.bank,
            id(ply.waveArc): ply.waveArc,
        }

    def snapshot(self):
        """An opaque, restore-able copy of the full render state at self.emitted."""
        ply = copy.deepcopy(self.ply, self._share_memo(self.ply))
        return {
            'ply': ply,
            'rate': self.rate,
            'hold_seconds': self.hold_seconds,
            'seconds_into': self.seconds_into,
            'next_clock': self.next_clock,
            'total': self.total,
            'emitted': self.emitted,
            'recording': self.recording,
            'tick0_ran': self.tick0_ran,
            'ended_at': self.ended_at,
            'finished': self.finished,
        }

    @classmethod
    def from_snapshot(cls, snap):
        """Build renderer from snapshot (copies Player, snapshot reusable)."""
        r = cls.__new__(cls)
        r.rate = snap['rate']
        r.hold_seconds = snap['hold_seconds']
        r.sps = 1.0 / snap['rate']
        r.ply = copy.deepcopy(snap['ply'], cls._share_memo(snap['ply']))
        r.seconds_into = snap['seconds_into']
        r.next_clock = snap['next_clock']
        r.total = snap['total']
        r.emitted = snap['emitted']
        r.recording = snap['recording']
        r.tick0_ran = snap['tick0_ran']
        r.ended_at = snap['ended_at']
        r.finished = snap['finished']
        return r

    @property
    def loop_marks(self):
        ply = self.ply
        if ply.loop_start_sample is not None and ply.loop_end_sample > ply.loop_start_sample:
            return ply.loop_start_sample, ply.loop_end_sample
        return None

    def step(self, produce=True):
        """
        Advance one driver clock.

        Returns:
            int16 (n, 2) ndarray when producing + past warm-up gate, else None.
        Sets self.finished at natural render end.
        """
        ply = self.ply
        # sequencer frozen (TEMPO 0): no tick can ever run again, so tracks
        # that are not ended never will be -- exact condition, not a time cap
        frozen = ply.tempo == 0 and ply.tempoCount < 240
        if not ply.any_channel_active() and (ply.all_tracks_ended() or frozen):
            self.finished = True
            return None
        # "idle": every track has ended, is suspended waiting for an endless
        # note's channel to die, or can never run again
        idle = frozen or all(
            ply.tracks[t].state[TS_END] or ply.tracks[t].waitChn for t in ply.trackIds
        )
        if idle:
            if self.ended_at is None:
                self.ended_at = self.total
            elif self.total - self.ended_at > self.hold_seconds * self.rate:
                # release ringing infinite notes (looped samples, PSG, noise);
                # one-shot PCM notes die on their own at the end of the sample
                for c in ply.channels:
                    if (
                        c.state > CS_START
                        and c.state < CS_RELEASE
                        and c.noteLength <= 0
                        and (c.reg.format == 3 or c.reg.repeatMode == 1)
                    ):
                        c.release()
        else:
            self.ended_at = None

        n = int((self.next_clock - self.seconds_into) / self.sps) + 1
        if n < 1:
            n = 1

        ply.now_sample = self.emitted
        bl = None
        br = None
        for chn in ply.channels:
            if chn.state == CS_NONE:
                continue
            chn.kill_after_block = False
            chn.cut_at_wrap = chn.noteLength <= 0 and CS_ATTACK <= chn.state <= CS_SUSTAIN
            res = chn.generate_block(n, produce)
            if res is not None:
                l, r = res
                if bl is None:
                    bl = l.copy()
                    br = r.copy()
                else:
                    bl += l
                    br += r
            if chn.kill_after_block:
                chn.kill()
        out = None
        if self.recording:
            if produce:
                if bl is None:
                    bl = np.zeros(n, dtype=np.int64)
                    br = np.zeros(n, dtype=np.int64)
                out = np.stack([np.clip(bl, -32768, 32767), np.clip(br, -32768, 32767)], axis=1).astype(np.int16)
            self.emitted += n
        self.total += n
        self.seconds_into += n * self.sps
        ply.now_sample = self.emitted
        ply.timer()
        if self.tick0_ran:
            self.recording = True
        self.tick0_ran = ply.ticked
        self.next_clock += SECONDS_PER_CLOCK
        return out

    def fast_forward_to(self, target_sample):
        """
        Silent ffwd until emitted >= target_sample (or render ends).

        Clock-granular: lands within one driver clock (~5ms) of target,
        never before.
        """
        while not self.finished and self.emitted < target_sample:
            self.step(produce=False)
        return self.emitted


def render_entry_stream(
    blob,
    start_offset,
    bank,
    waveArc,
    entry_volume,
    rate,
    hold_seconds=2.0,
    player_prio=0,
    chunk_samples=STREAM_CHUNK_SAMPLES,
):
    """
    Render one SSAR/SSEQ entry incrementally.

    Yields ('data', int16 stereo ndarray) chunks (~STREAM_CHUNK_SAMPLES), then ('end', looped: bool, loop_marks or None). Regroups
    LiveRenderer.step() — concatenated whole is bit-identical to a one-shot render.
    """
    r = LiveRenderer(blob, start_offset, bank, waveArc, entry_volume, rate, hold_seconds, player_prio)
    pend = []
    pend_n = 0
    while not r.finished:
        out = r.step(produce=True)
        if out is not None:
            pend.append(out)
            pend_n += len(out)
            if pend_n >= chunk_samples:
                yield 'data', np.concatenate(pend)
                pend = []
                pend_n = 0
    if pend_n:
        yield 'data', np.concatenate(pend)
    yield 'end', r.ply.loop_detected, r.loop_marks


def estimate_end_sample(blob, start_offset, rate, player_prio=0, should_abort=None, abort_every=4096):
    """
    Sample index where the sequencer finishes (all tracks ended/frozen).

    Runs the REAL sequencer with an empty bank — no note can allocate a channel, so only track/tempo/loop logic runs (~500-1000× faster than full render). Lower bound (release tails excluded); snap to exact total at render finalize.
    Clock/emit arithmetic mirrors render_entry_stream (including warm-up gate) > sample-for-sample alignment.

    Args:
        should_abort: callable polled every abort_every clocks; return True
            to abort (returns None).
    """
    ply = Player(blob, [], [None, None, None, None], rate, 0, player_prio)
    ply.setup(start_offset)
    ply.timer()

    sps = 1.0 / rate
    seconds_into = 0.0
    next_clock = SECONDS_PER_CLOCK
    emitted = 0
    recording = False
    tick0_ran = False
    clocks = 0
    while True:
        frozen = ply.tempo == 0 and ply.tempoCount < 240
        if ply.all_tracks_ended() or frozen:
            return emitted
        n = int((next_clock - seconds_into) / sps) + 1
        if n < 1:
            n = 1
        if recording:
            emitted += n
        seconds_into += n * sps
        ply.now_sample = emitted
        ply.timer()
        if tick0_ran:
            recording = True
        tick0_ran = ply.ticked
        next_clock += SECONDS_PER_CLOCK
        clocks += 1
        if should_abort is not None and clocks % abort_every == 0 and should_abort():
            return None

def render_entry(blob, start_offset, bank, waveArc, entry_volume, rate, hold_seconds=2.0, player_prio=0):
    """
    Render one SSAR/SSEQ entry → single stereo int16 buffer.

    Returns:
        (stereo int16 ndarray, looped: bool, loop_marks: (start, end) or None).
    Bit-identical to render_entry_stream concatenation.
    """
    chunks = []
    looped = False
    marks = None
    for item in render_entry_stream(
        blob, start_offset, bank, waveArc, entry_volume, rate, hold_seconds, player_prio
    ):
        if item[0] == 'data':
            chunks.append(item[1])
        else:
            looped, marks = item[1], item[2]
    if not chunks:
        return np.zeros((0, 2), dtype=np.int16), looped, marks
    return np.concatenate(chunks), looped, marks
