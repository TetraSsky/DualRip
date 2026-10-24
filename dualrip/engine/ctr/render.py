"""3DS render entry points."""

import copy
import numpy as np
from .cprims import (
    AMPL_K, AMPL_THRESHOLD, CS_NONE, CS_RELEASE, DRIVER_HZ,
    cdiv, cnv_attack, cnv_fall, cnv_sust,
)
from .sequencer import CseqPlayer

def _int16_stereo(left, right):
    """Float sample lists to an int16 stereo block, clipped like the one-shot path."""
    out = np.empty((len(left), 2), dtype=np.int16)
    out[:, 0] = np.clip(left, -32768, 32767)
    out[:, 1] = np.clip(right, -32768, 32767)
    return out

class LiveRenderer:
    """Resumable, seekable driver for one CSEQ entry."""

    def __init__(self, blob, start_offset, bank_lookup, rate, channel_prio, hold_seconds=2.0, loop_passes=2, base_vol=127, effects=None):
        self.rate = rate
        self.hold_seconds = hold_seconds
        self.channel_prio = channel_prio
        self.loop_passes = loop_passes
        self.base_vol = base_vol
        self.effects = effects
        self.ply = CseqPlayer(blob, bank_lookup, rate, channel_prio, loop_passes, base_vol, effects)
        self.ply.setup(start_offset)
        self.ply.timer()
        self.samples_per_clock = rate / DRIVER_HZ
        self.frac = 0.0
        self.total = 0 # samples advanced
        self.emitted = 0 # recorded samples
        self.recording = False
        self.tick0_ran = False
        self.idle_clocks = 0
        self.finished = False

    @staticmethod
    def _share_memo(ply):
        """Deepcopy memo keeping the sequence bytes and the sample arrays shared."""
        memo = {id(ply.blob): ply.blob}
        for v in ply.voices:
            samples = getattr(v, 'samples', None)
            if isinstance(samples, np.ndarray):
                memo[id(samples)] = samples
        return memo

    def snapshot(self):
        """Restorable copy of the render state."""
        return {
            'ply': copy.deepcopy(self.ply, self._share_memo(self.ply)),
            'rate': self.rate,
            'hold_seconds': self.hold_seconds,
            'channel_prio': self.channel_prio,
            'loop_passes': self.loop_passes,
            'base_vol': self.base_vol,
            'effects': self.effects,
            'samples_per_clock': self.samples_per_clock,
            'frac': self.frac,
            'total': self.total,
            'emitted': self.emitted,
            'recording': self.recording,
            'tick0_ran': self.tick0_ran,
            'idle_clocks': self.idle_clocks,
            'finished': self.finished,
        }

    @classmethod
    def from_snapshot(cls, snap):
        """Renderer rebuilt from a snapshot."""
        r = cls.__new__(cls)
        r.rate = snap['rate']
        r.hold_seconds = snap['hold_seconds']
        r.channel_prio = snap['channel_prio']
        r.loop_passes = snap['loop_passes']
        r.base_vol = snap['base_vol']
        r.effects = snap['effects']
        r.ply = copy.deepcopy(snap['ply'], cls._share_memo(snap['ply']))
        r.samples_per_clock = snap['samples_per_clock']
        r.frac = snap['frac']
        r.total = snap['total']
        r.emitted = snap['emitted']
        r.recording = snap['recording']
        r.tick0_ran = snap['tick0_ran']
        r.idle_clocks = snap['idle_clocks']
        r.finished = snap['finished']
        return r

    @property
    def loop_marks(self):
        ply = self.ply
        if ply.loop_detected and ply.loop_start_sample is not None:
            return ply.loop_start_sample, ply.loop_end_sample or self.emitted
        return None

    def step(self, produce=True):
        """Advance one driver clock; returns an int16 (n, 2) block or None."""
        ply = self.ply
        frozen = ply.tempo == 0 and ply.tempoCount < ply.tick_threshold()
        seq_done = ply.all_tracks_ended() or frozen
        if seq_done and not ply.any_voice_active():
            self.finished = True
            return None
        if seq_done:
            self.idle_clocks += 1
            if self.idle_clocks > int(self.hold_seconds * DRIVER_HZ):
                # endless notes only the game can stop
                for v in ply.voices:
                    if v.state > CS_NONE and v.state != CS_RELEASE:
                        v.release()
                if self.idle_clocks > int(self.hold_seconds * DRIVER_HZ) + int(5 * DRIVER_HZ):
                    self.finished = True
                    return None
        else:
            self.idle_clocks = 0

        self.frac += self.samples_per_clock
        n = int(self.frac)
        self.frac -= n
        ply.now_sample = self.emitted
        out = None
        if self.recording:
            if produce:
                l, r = ply.generate(n)
                out = _int16_stereo(l, r)
            else:
                ply.generate(n, produce=False)
            self.emitted += n
        else:
            if produce:
                ply.generate(n)
            else:
                ply.generate(n, produce=False)
            if self.tick0_ran:
                self.recording = True
                ply.now_sample = 0
        self.total += n
        if ply.ticked:
            self.tick0_ran = True
        ply.timer()
        return out

    def fast_forward_to(self, target_sample):
        """Silent fast-forward until emitted >= target_sample or the render ends."""
        while not self.finished and self.emitted < target_sample:
            self.step(produce=False)
        return self.emitted

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
