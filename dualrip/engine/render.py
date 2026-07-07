# Part of DualRip. Core playback logic is a faithful Python port of the FeOS
# Sound System (fincs), as adapted by Naram Qashat (CyberBotX) for the NCSF
# player (github.com/CyberBotX/in_xsf, src/in_ncsf/SSEQPlayer). Lookup tables
# come from disassembly of Nintendo's NNS sound driver by those authors.
# FIDELITY-CRITICAL: C integer semantics (truncating division, arithmetic
# shifts, table indexing) are intentional. Do not "simplify".

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


def render_entry(blob, start_offset, bank, waveArc, entry_volume, rate, hold_seconds=2.0):
    """Render one SSAR entry in raw form.
    Returns (stereo int16, looped: bool, loop_marks: (start, end) samples or None).

    Raw = one iteration of every loop, full release envelopes, native rests
    preserved. PSG/noise endless notes released hold_seconds after idle.
    Only the fixed ~15 ms sequencer warm-up is skipped.
    """
    ply = Player(blob, bank, waveArc, rate, cnv_scale(entry_volume))
    ply.setup(start_offset)
    ply.timer()

    sps = 1.0 / rate
    seconds_into = 0.0
    next_clock = SECONDS_PER_CLOCK
    out_l = []
    out_r = []
    total = 0
    emitted = 0
    # Cold-start gate. On hardware the driver is already running when a sound
    # is triggered, so tick 0 sounds immediately; the emulator instead starts
    # from tempoCount 0 (clocks until the first tick runs) and needs one more
    # timer() for channel updates to arm the tick-0 note-ons. Blocks rendered
    # before that point are pure emulation warm-up -- discard them, record
    # from the first block that can carry tick-0 audio. Everything after
    # (native rests included) is sequence content.
    recording = False
    tick0_ran = False  # ply.ticked observed before the latest timer() call
    ended_at = None

    while True:
        # sequencer frozen (TEMPO 0): no tick can ever run again, so tracks
        # that are not ended never will be -- exact condition, not a time cap
        frozen = ply.tempo == 0 and ply.tempoCount < 240
        if not ply.any_channel_active() and (ply.all_tracks_ended() or frozen):
            break
        # "idle": every track has ended, is suspended waiting for an endless
        # note's channel to die, or can never run again
        idle = frozen or all(
            ply.tracks[t].state[TS_END] or ply.tracks[t].waitChn for t in ply.trackIds
        )
        if idle:
            if ended_at is None:
                ended_at = total
            elif total - ended_at > hold_seconds * rate:
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
            ended_at = None

        n = int((next_clock - seconds_into) / sps) + 1
        if n < 1:
            n = 1

        ply.now_sample = emitted
        bl = None
        br = None
        for chn in ply.channels:
            if chn.state == CS_NONE:
                continue
            chn.kill_after_block = False
            chn.cut_at_wrap = chn.noteLength <= 0 and CS_ATTACK <= chn.state <= CS_SUSTAIN
            res = chn.generate_block(n)
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
        if recording:
            if bl is None:
                bl = np.zeros(n, dtype=np.int64)
                br = np.zeros(n, dtype=np.int64)
            out_l.append(bl)
            out_r.append(br)
            emitted += n
        total += n
        seconds_into += n * sps
        ply.now_sample = emitted
        ply.timer()
        # ply.ticked latches once the first tick has run; the timer() call
        # right after that (this one, when tick0_ran is already set) is the
        # channel update that arms the tick-0 note-ons, so the next block is
        # the first audible one.
        if tick0_ran:
            recording = True
        tick0_ran = ply.ticked
        next_clock += SECONDS_PER_CLOCK

    marks = None
    if ply.loop_start_sample is not None and ply.loop_end_sample > ply.loop_start_sample:
        marks = (ply.loop_start_sample, ply.loop_end_sample)
    if not out_l:
        return np.zeros((0, 2), dtype=np.int16), ply.loop_detected, marks
    left = np.concatenate(out_l)
    right = np.concatenate(out_r)
    stereo = np.stack(
        [np.clip(left, -32768, 32767), np.clip(right, -32768, 32767)], axis=1
    ).astype(np.int16)
    return stereo, ply.loop_detected, marks
