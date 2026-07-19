"""3DS render entry points."""

from .cprims import (
    AMPL_K, AMPL_THRESHOLD, CS_NONE, CS_RELEASE, DRIVER_HZ,
    cdiv, cnv_attack, cnv_fall, cnv_sust,
)
from .sequencer import CseqPlayer

def render_entry(blob, start_offset, bank_lookup, rate, channel_prio, hold_seconds=2.0, loop_passes=2, base_vol=127, effects=None):
    """Render one CSEQ entry to (chans, loop, unapplied)."""
    ply = CseqPlayer(blob, bank_lookup, rate, channel_prio, loop_passes, base_vol, effects)
    ply.setup(start_offset)
    ply.timer()

    spc = 1.0 / DRIVER_HZ
    samples_per_clock = rate / DRIVER_HZ
    out_l = []
    out_r = []
    recording = False
    tick0_ran = False
    idle_clocks = 0
    hold_clocks = int(hold_seconds * DRIVER_HZ)
    frac = 0.0

    while True:
        frozen = ply.tempo == 0 and ply.tempoCount < ply.tick_threshold()
        seq_done = ply.all_tracks_ended() or frozen
        if seq_done and not ply.any_voice_active():
            break
        if seq_done:
            idle_clocks += 1
            if idle_clocks > hold_clocks:
                # endless notes only the game can stop: release and drain
                for v in ply.voices:
                    if v.state > CS_NONE and v.state != CS_RELEASE:
                        v.release()
                if idle_clocks > hold_clocks + int(5 * DRIVER_HZ):
                    break
        frac += samples_per_clock
        n = int(frac)
        frac -= n
        if recording:
            l, r = ply.generate(n)
            out_l.extend(l)
            out_r.extend(r)
        else:
            ply.generate(n)
            if tick0_ran:
                recording = True
                ply.now_sample = 0
        if ply.ticked:
            tick0_ran = True
        ply.timer()

    chans = [[max(-32768, min(32767, int(x))) for x in out_l], [max(-32768, min(32767, int(x))) for x in out_r]]
    loop = None
    if ply.loop_detected and ply.loop_start_sample is not None:
        loop = (ply.loop_start_sample, ply.loop_end_sample or len(chans[0]))
    return chans, loop, ply.unapplied

def render_wsd(ctx, cwsd, item_index, rate, base_vol):
    """Render one wave-sound item to per-channel int16."""
    item = cwsd.items[item_index]
    if item is None:
        raise LookupError('null WSD item %d' % item_index)
    events = item.events or [(0.0, 0.0, i) for i in range(len(item.notes))]
    rendered = []
    for pos_f, _len_f, idx in events:
        note = item.notes[idx]
        if note is None:
            continue
        cwav = ctx.cwav(*cwsd.waves[note.wave_index])
        ratio = note.pitch * item.pitch
        inc = cwav.rate * ratio / rate
        smp = cwav.samples
        ln = len(smp)
        total = int(ln / inc) if not cwav.loop else int(ln / inc)
        # A/D/S envelope in dB
        vol_db = (cnv_sust(base_vol) + cnv_sust(note.volume) + cnv_sust(127))
        attackLvl = cnv_attack(note.attack)
        decayRate = cnv_fall(note.decay)
        sustLvl = cnv_sust(note.sustain) << 7
        ampl = AMPL_THRESHOLD
        state = 'a'
        pan = max(0, min(127, (note.pan - 64) + (item.pan - 64) + 64))
        out = [0.0] * total
        clock_samples = rate / DRIVER_HZ
        next_clock = 0.0
        amp = 0.0
        p = 0.0
        for i in range(total):
            if i >= next_clock:
                if state == 'a':
                    new = ampl
                    old = ampl >> 7
                    while True:
                        new = cdiv(new * attackLvl, 256)
                        if (new >> 7) != old:
                            break
                    ampl = new
                    if not ampl:
                        state = 'd'
                elif state == 'd':
                    ampl -= decayRate
                    if ampl <= sustLvl:
                        ampl = sustLvl
                        state = 's'
                total_db = max(-AMPL_K, min(0, (ampl >> 7) + vol_db))
                amp = 10.0 ** (total_db / 160.0)
                next_clock += clock_samples
            ip = int(p)
            if ip >= ln:
                out = out[:i]
                break
            frac = p - ip
            s0 = smp[ip]
            s1 = smp[ip + 1] if ip + 1 < ln else 0
            out[i] = (s0 + (s1 - s0) * frac) * amp
            p += inc
        vol_l = 1.0 if pan <= 64 else (127 - pan) / 63.0
        vol_r = 1.0 if pan >= 64 else pan / 64.0
        rendered.append((pos_f, out, vol_l, vol_r))
    # mix each event at its position (seconds)
    end = 0
    for pos_f, out, _, _ in rendered:
        end = max(end, int(pos_f * rate) + len(out))
    left = [0.0] * end
    right = [0.0] * end
    for pos_f, out, vl, vr in rendered:
        o = int(pos_f * rate)
        for i, s in enumerate(out):
            left[o + i] += s * vl
            right[o + i] += s * vr
    chans = [[max(-32768, min(32767, int(x))) for x in left], [max(-32768, min(32767, int(x))) for x in right]]
    return chans
