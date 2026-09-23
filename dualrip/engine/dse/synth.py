"""DSE synthesis: a sequencer driving a per-note sampler with an ADSR envelope."""

import numpy as np

# envelope duration lookups, indexed by the 0-127 parameter (in ms)
ENV_16 = [
    0x0000, 0x0001, 0x0002, 0x0003, 0x0004, 0x0005, 0x0006, 0x0007, 0x0008,
    0x0009, 0x000A, 0x000B, 0x000C, 0x000D, 0x000E, 0x000F, 0x0010, 0x0011,
    0x0012, 0x0013, 0x0014, 0x0015, 0x0016, 0x0017, 0x0018, 0x0019, 0x001A,
    0x001B, 0x001C, 0x001D, 0x001E, 0x001F, 0x0020, 0x0023, 0x0028, 0x002D,
    0x0033, 0x0039, 0x0040, 0x0048, 0x0050, 0x0058, 0x0062, 0x006D, 0x0078,
    0x0083, 0x0090, 0x009E, 0x00AC, 0x00BC, 0x00CC, 0x00DE, 0x00F0, 0x0104,
    0x0119, 0x012F, 0x0147, 0x0160, 0x017A, 0x0196, 0x01B3, 0x01D2, 0x01F2,
    0x0214, 0x0238, 0x025E, 0x0285, 0x02AE, 0x02D9, 0x0307, 0x0336, 0x0367,
    0x039B, 0x03D1, 0x0406, 0x0442, 0x047E, 0x04C4, 0x0500, 0x0546, 0x058C,
    0x0622, 0x0672, 0x06CC, 0x071C, 0x0776, 0x07DA, 0x0834, 0x0898, 0x0906,
    0x096A, 0x09D8, 0x0A50, 0x0ABE, 0x0B40, 0x0BB8, 0x0C3A, 0x0CBC, 0x0D48,
    0x0DDE, 0x0E6A, 0x0F00, 0x0FA0, 0x1040, 0x10EA, 0x1194, 0x123E, 0x12F2,
    0x13B0, 0x146E, 0x1536, 0x15FE, 0x16D0, 0x17A2, 0x187E, 0x195A, 0x1A40,
    0x1B30, 0x1C20, 0x1D1A, 0x1E1E, 0x1F22, 0x2030, 0x2148, 0x2260, 0x2382,
    0x2710, 0x7FFF]

ENV_32 = [
    0x00000000, 0x00000004, 0x00000007, 0x0000000A, 0x0000000F, 0x00000015,
    0x0000001C, 0x00000024, 0x0000002E, 0x0000003A, 0x00000048, 0x00000057,
    0x00000068, 0x0000007B, 0x00000091, 0x000000A8, 0x00000185, 0x000001BE,
    0x000001FC, 0x0000023F, 0x00000288, 0x000002D6, 0x0000032A, 0x00000385,
    0x000003E5, 0x0000044C, 0x000004BA, 0x0000052E, 0x000005A9, 0x0000062C,
    0x000006B5, 0x00000746, 0x00000BCF, 0x00000CC0, 0x00000DBD, 0x00000EC6,
    0x00000FDC, 0x000010FF, 0x0000122F, 0x0000136C, 0x000014B6, 0x0000160F,
    0x00001775, 0x000018EA, 0x00001A6D, 0x00001BFF, 0x00001DA0, 0x00001F51,
    0x00002C16, 0x00002E80, 0x00003100, 0x00003395, 0x00003641, 0x00003902,
    0x00003BDB, 0x00003ECA, 0x000041D0, 0x000044EE, 0x00004824, 0x00004B73,
    0x00004ED9, 0x00005259, 0x000055F2, 0x000059A4, 0x000074CC, 0x000079AB,
    0x00007EAC, 0x000083CE, 0x00008911, 0x00008E77, 0x000093FF, 0x000099AA,
    0x00009F78, 0x0000A56A, 0x0000AB80, 0x0000B1BB, 0x0000B81A, 0x0000BE9E,
    0x0000C547, 0x0000CC17, 0x0000FD42, 0x000105CB, 0x00010E82, 0x00011768,
    0x0001207E, 0x000129C4, 0x0001333B, 0x00013CE2, 0x000146BB, 0x000150C5,
    0x00015B02, 0x00016572, 0x00017015, 0x00017AEB, 0x000185F5, 0x00019133,
    0x0001E16D, 0x0001EF07, 0x0001FCE0, 0x00020AF7, 0x0002194F, 0x000227E6,
    0x000236BE, 0x000245D7, 0x00025532, 0x000264CF, 0x000274AE, 0x000284D0,
    0x00029536, 0x0002A5E0, 0x0002B6CE, 0x0002C802, 0x000341B0, 0x000355F8,
    0x00036A90, 0x00037F79, 0x000394B4, 0x0003AA41, 0x0003C021, 0x0003D654,
    0x0003ECDA, 0x000403B5, 0x00041AE5, 0x0004326A, 0x00044A45, 0x00046277,
    0x00047B00, 0x7FFFFFFF]

def env_ms(param, mult):
    """Envelope duration parameter to milliseconds."""
    return (ENV_32 if mult == 0 else ENV_16)[param & 0x7F]

class Voice:
    """One sounding note: a sample at a pitch, shaped by the ADSR envelope."""

    def __init__(self, sample, split, semitones, amp, pan, out_rate):
        self.pcm = sample.pcm().astype(np.float32)
        self.loop = sample.loop_enabled and sample.loop_len > 0
        self.loop_beg = sample.loop_beg
        self.loop_end = sample.loop_beg + sample.loop_len
        self.base_step = (sample.sample_rate / out_rate) * (2.0 ** (semitones / 12.0))
        self.step = self.base_step
        self.pos = 0.0
        # quadratic volume curve (driver squares combined volume)
        self.amp2 = amp * amp
        self.pan_l = (127 - pan) / 128.0
        self.pan_r = pan / 128.0
        self.tid = -1
        self.dead = False

        def dur(param):
            return max(1, int(env_ms(param, split.env_mult) * out_rate / 1000))
        if split.env_on:
            a = dur(split.attack)
            h = dur(split.hold)
            d = dur(split.decay)
            atk_l = max(0.0, min(1.0, (split.atk_vol & 0x7F) / 127.0))
            sus_l = max(0.0, min(1.0, (split.sustain & 0x7F) / 127.0))
            t = [0, a, a + h, a + h + d]
            lv = [atk_l, 1.0, 1.0, sus_l]
            if (split.decay2 & 0x7F) < 0x7F:
                t.append(a + h + d + dur(split.decay2))
                lv.append(0.0)
            else:
                t.append(t[-1] + 1)
                lv.append(sus_l)
            self.release_s = dur(split.release)
        else:
            t = [0, max(1, int(0.004 * out_rate))]
            lv = [0.0, 1.0]
            self.release_s = max(1, int(0.08 * out_rate))
        self.env_t = np.array(t, dtype=np.float64)
        self.env_l = np.array(lv, dtype=np.float32)
        self.epos = 0
        self.rel_at = None
        self.rel_level = 0.0

    def _held_level(self, times):
        return np.interp(times, self.env_t, self.env_l).astype(np.float32)

    def render(self, n, buf):
        if self.dead:
            return
        pos = self.pos + self.step * np.arange(n)
        last = pos[-1] + self.step
        if self.loop:
            over = pos >= self.loop_end
            if over.any():
                pos = pos.copy()
                span = self.loop_end - self.loop_beg
                pos[over] = self.loop_beg + np.mod(pos[over] - self.loop_beg, span)
            alive = n
        else:
            alive = int((pos < len(self.pcm) - 1).sum())
            pos = pos[:alive]
        if len(pos) == 0:
            self.dead = True
            return
        idx = np.clip(pos.astype(np.int64), 0, len(self.pcm) - 2)
        frac = pos - idx
        samp = self.pcm[idx] * (1.0 - frac) + self.pcm[idx + 1] * frac

        m = len(samp)
        et = self.epos + np.arange(m)
        if self.rel_at is None:
            env = self._held_level(et)
        else:
            fade = np.clip(1.0 - (et - self.rel_at) / self.release_s, 0.0, 1.0)
            env = self.rel_level * fade
        self.epos += n
        gain = self.amp2 * (env * env)
        samp = samp * gain
        buf[:m, 0] += samp * self.pan_l
        buf[:m, 1] += samp * self.pan_r
        self.pos = last
        if self.rel_at is not None and (self.epos - self.rel_at) >= self.release_s:
            self.dead = True
        if not self.loop and alive < n:
            self.dead = True

    def set_bend(self, bend_semitones):
        self.step = self.base_step * (2.0 ** (bend_semitones / 12.0))

    def release(self):
        if self.rel_at is None:
            self.rel_at = self.epos
            self.rel_level = float(self._held_level(np.array([self.epos]))[0])

def render(events, tpqn, ntrk, preset, bank, out_rate=32728, master=0.7, drain_held=False):
    """Render a sequence of events to a stereo audio buffer."""
    prog = [0] * ntrk
    vol = [127] * ntrk
    expr = [127] * ntrk
    pan = [64] * ntrk
    bend = [0] * ntrk
    bpm = 120
    voices = []
    active = {}
    blocks = []

    def pick_split(program, midi, vel):
        pr = preset.programs.get(program)
        if pr is None:
            return None
        for sp in pr.splits:
            if sp.low_key <= midi <= sp.high_key and sp.low_vel <= vel <= sp.high_vel:
                return pr, sp
        return (pr, pr.splits[0]) if pr.splits else None

    def render_gap(nsamp):
        if nsamp <= 0:
            return
        buf = np.zeros((nsamp, 2), dtype=np.float32)
        for v in voices:
            v.render(nsamp, buf)
        blocks.append(buf)

    cur_tick = 0
    for ev in events:
        dtick = ev[0] - cur_tick
        if dtick > 0:
            render_gap(int(out_rate * dtick * 60 / (bpm * tpqn)))
            cur_tick = ev[0]
            voices[:] = [v for v in voices if not v.dead]
        kind = ev[1]
        if kind == 'tempo':
            bpm = ev[2] or bpm
        elif kind == 'prog':
            prog[ev[2]] = ev[3]
        elif kind == 'vol':
            vol[ev[2]] = ev[3]
        elif kind == 'expr':
            expr[ev[2]] = ev[3]
        elif kind == 'pan':
            pan[ev[2]] = ev[3]
        elif kind == 'bend':
            bend[ev[2]] = ev[3]
        elif kind == 'on':
            _t, _k, tid, midi, vel = ev
            picked = pick_split(prog[tid], midi, vel)
            if picked is None:
                continue
            pr, sp = picked
            sample = bank.samples.get(sp.sample_id)
            if sample is None or sample.raw is None:
                continue
            semitones = midi - sp.root_key # carries coarse+fine tuning
            amp = (vel / 127.0) * (sp.volume / 127.0 if sp.volume >= 0 else 1.0)
            amp *= (pr.volume / 127.0) * (vol[tid] / 127.0) * (expr[tid] / 127.0)
            v = Voice(sample, sp, semitones, amp, pan[tid], out_rate)
            v.tid = tid
            key = (tid, midi)
            if key in active:
                active[key].release()
            active[key] = v
            voices.append(v)
        elif kind == 'off':
            _t, _k, tid, midi = ev
            v = active.pop((tid, midi), None)
            if v is not None:
                v.release()

    render_gap(int(out_rate * 1.5)) # tail for releases
    if drain_held:
        # ring out held one-shot voices to their natural end; cap bounds looping holds
        voices[:] = [v for v in voices if not v.dead]
        chunk = int(out_rate * 0.25)
        cap = int(out_rate * 18)
        drained = 0
        while voices and drained < cap:
            render_gap(chunk)
            drained += chunk
            voices[:] = [v for v in voices if not v.dead]
    if not blocks:
        return np.zeros((0, 2), dtype=np.int16)
    audio = np.concatenate(blocks, axis=0)
    audio *= master
    np.clip(audio, -32768, 32767, out=audio)
    return audio.astype(np.int16)
