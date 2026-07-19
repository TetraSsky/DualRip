"""3DS CSEQ sequencer: opcode grammar, CseqPlayer and CseqTrack interpreter."""

import math

from .cprims import (
    AMPL_K, AMPL_THRESHOLD,
    CS_NONE, CS_START, CS_ATTACK, CS_DECAY, CS_SUSTAIN, CS_RELEASE,
    TS_NOTEWAIT, TS_PORTA, TS_TIE, TS_END,
    cdiv, s8, s16, cnv_sine, cnv_attack, cnv_fall, cnv_sust, readvl, Rng,
)
from .channel import Voice

# CSEQ command argument layouts (big-endian multi-byte args)
ARG_NONE, ARG_U8, ARG_S16, ARG_U16, ARG_VL, ARG_U8_U24, ARG_U24 = range(7)

CSEQ_ARGS = {
    0x80: ARG_VL, 0x81: ARG_VL, # wait, program
    0x88: ARG_U8_U24, 0x89: ARG_U24, 0x8A: ARG_U24, # opentrack, jump, call
    0xB0: ARG_U8, 0xB1: ARG_U8, 0xB2: ARG_U8, 0xB3: ARG_U8,
    0xB4: ARG_U8, 0xB5: ARG_U8, 0xB6: ARG_U8,
    0xBD: ARG_U8, 0xBE: ARG_U8, 0xBF: ARG_U8,
    0xC0: ARG_U8, 0xC1: ARG_U8, 0xC2: ARG_U8, 0xC3: ARG_U8,
    0xC4: ARG_U8, 0xC5: ARG_U8, 0xC6: ARG_U8, 0xC7: ARG_U8,
    0xC8: ARG_U8, 0xC9: ARG_U8, 0xCA: ARG_U8, 0xCB: ARG_U8,
    0xCC: ARG_U8, 0xCD: ARG_U8, 0xCE: ARG_U8, 0xCF: ARG_U8,
    0xD0: ARG_U8, 0xD1: ARG_U8, 0xD2: ARG_U8, 0xD3: ARG_U8,
    0xD4: ARG_U8, 0xD5: ARG_U8, 0xD6: ARG_U8, 0xD7: ARG_U8,
    0xD8: ARG_U8, 0xD9: ARG_U8, 0xDA: ARG_U8, 0xDB: ARG_U8,
    0xDC: ARG_U8, 0xDD: ARG_U8, 0xDE: ARG_U8, 0xDF: ARG_U8,
    0xE0: ARG_S16, 0xE1: ARG_S16, 0xE3: ARG_S16, 0xE4: ARG_S16,
    0xFB: ARG_NONE, 0xFC: ARG_NONE, 0xFD: ARG_NONE,
    0xFE: ARG_U16, 0xFF: ARG_NONE,
}
CSEQ_NAMES = {
    0x80: 'wait', 0x81: 'prg', 0x88: 'opentrack', 0x89: 'jump', 0x8A: 'call',
    0xA0: '_r', 0xA1: '_v', 0xA2: '_if', 0xA3: '_t', 0xA4: '_tr', 0xA5: '_tv',
    0xB0: 'timebase', 0xB1: 'env_hold', 0xB2: 'monophonic', 0xB3: 'velocity_range',
    0xB4: 'biquad_type', 0xB5: 'biquad_value', 0xB6: 'bank_select',
    0xBD: 'mod_phase', 0xBE: 'mod_curve', 0xBF: 'front_bypass',
    0xC0: 'pan', 0xC1: 'volume', 0xC2: 'main_volume', 0xC3: 'transpose',
    0xC4: 'pitchbend', 0xC5: 'bendrange', 0xC6: 'prio', 0xC7: 'notewait',
    0xC8: 'tie', 0xC9: 'porta', 0xCA: 'mod_depth', 0xCB: 'mod_speed',
    0xCC: 'mod_type', 0xCD: 'mod_range', 0xCE: 'porta_sw', 0xCF: 'porta_time',
    0xD0: 'attack', 0xD1: 'decay', 0xD2: 'sustain', 0xD3: 'release',
    0xD4: 'loop_start', 0xD5: 'volume2', 0xD6: 'printvar', 0xD7: 'span',
    0xD8: 'lpf_cutoff', 0xD9: 'fxsend_a', 0xDA: 'fxsend_b', 0xDB: 'mainsend',
    0xDC: 'init_pan', 0xDD: 'mute', 0xDE: 'fxsend_c', 0xDF: 'damper',
    0xE0: 'mod_delay', 0xE1: 'tempo', 0xE3: 'sweep_pitch', 0xE4: 'mod_period',
    0xF0: 'ext', 0xFB: 'env_reset', 0xFC: 'loop_end', 0xFD: 'ret',
    0xFE: 'alloctrack', 0xFF: 'fin',
}

def skip_command(blob, pos):
    """Advance past one full command, prefixes included."""
    cmd = blob[pos]
    pos += 1
    if cmd < 0x80: # note: velocity + varlen length
        pos += 1
        _, pos = readvl(blob, pos)
        return pos
    if cmd == 0xA0: # random prefix: inner(no arg) + min/max s16
        pos = _skip_inner_noarg(blob, pos)
        return pos + 4
    if cmd == 0xA1: # variable prefix: inner(no arg) + var u8
        pos = _skip_inner_noarg(blob, pos)
        return pos + 1
    if cmd == 0xA2: # if prefix: full inner command
        return skip_command(blob, pos)
    if cmd == 0xA3: # time prefix: full inner + s16 time
        return skip_command(blob, pos) + 2
    if cmd == 0xA4: # time-random prefix: full inner + min/max s16
        return skip_command(blob, pos) + 4
    if cmd == 0xA5: # time-variable prefix: full inner + var u8
        return skip_command(blob, pos) + 1
    if cmd == 0xF0: # extended: sub-op + type-dependent args
        return pos + 1 + _ext_arg_size(blob[pos])
    arg = CSEQ_ARGS.get(cmd)
    if arg is None:
        raise ValueError('unknown CSEQ command %#04x at %#x' % (cmd, pos - 1))
    if arg == ARG_U8:
        return pos + 1
    if arg in (ARG_S16, ARG_U16):
        return pos + 2
    if arg == ARG_VL:
        _, pos = readvl(blob, pos)
        return pos
    if arg == ARG_U24:
        return pos + 3
    if arg == ARG_U8_U24:
        return pos + 4
    return pos

def _ext_arg_size(sub):
    """Argument byte count of an extended (0xF0) sub-command."""
    if 0x80 <= sub <= 0x95: # variable ops / comparisons: u8 + s16
        return 3
    if 0xA0 <= sub <= 0xB1: # mod2-4 u8 params
        return 1
    if 0xE0 <= sub <= 0xE6: # usercall / mod delays-periods: s16
        return 2
    raise ValueError('unknown extended sub-command %#04x' % sub)

def _skip_inner_noarg(blob, pos):
    """Skip an inner command in no-parameter mode, only its fixed lead bytes."""
    cmd = blob[pos]
    pos += 1
    if cmd < 0x80:
        return pos + 1 # velocity still read
    if cmd == 0x88:
        return pos + 1 # track number still read
    if cmd == 0xF0:
        sub = blob[pos]
        pos += 1
        if 0x80 <= sub <= 0x95:
            pos += 1 # variable index still read
        return pos
    return pos

def disassemble(blob, pos, out=None, follow=True):
    """Decode commands from pos until Fin, collecting (offset, text)."""
    lines = [] if out is None else out
    seen = set()
    stack = [pos]
    while stack:
        p = stack.pop()
        while p not in seen and 0 <= p < len(blob):
            seen.add(p)
            cmd = blob[p]
            np = skip_command(blob, p)
            if cmd < 0x80:
                txt = 'note %d vel %d len %s' % (cmd, blob[p + 1], readvl(blob, p + 2)[0])
            elif cmd == 0x88:
                t = blob[p + 1]
                dest = (blob[p + 2] << 16) | (blob[p + 3] << 8) | blob[p + 4]
                txt = 'opentrack %d -> %#x' % (t, dest)
                if follow:
                    stack.append(dest)
            elif cmd in (0x89, 0x8A):
                dest = (blob[p + 1] << 16) | (blob[p + 2] << 8) | blob[p + 3]
                txt = '%s %#x' % (CSEQ_NAMES[cmd], dest)
                if follow:
                    stack.append(dest)
            else:
                txt = CSEQ_NAMES.get(cmd, '?%02x' % cmd)
            lines.append((p, txt))
            if cmd == 0xFF:
                break
            p = np
    return lines

class CseqPlayer:
    """CSEQ player."""

    NUM_VOICES = 24

    def __init__(self, blob, bank_lookup, out_rate, channel_prio=64, loop_passes=1, base_vol=127, effects=None):
        self.blob = blob
        self.bank_lookup = bank_lookup # (bank_no, program, key, vel) -> (VelRegion, Cwav)
        self.rate = out_rate
        self.channel_prio = channel_prio
        self.baseVol = cnv_sust(base_vol) # entry volume, dB
        # aux buses by id
        self.effects = effects or {}
        self.loop_passes = loop_passes
        self.loop_detected = False
        self.loop_start_sample = None
        self.loop_end_sample = None
        self.tempo = 120
        self.tempoCount = 0
        self.timebase = 48
        self.masterVol = 0
        self.ticked = False
        self.now_sample = 0
        self.variables = [-1] * 32
        self.rng = Rng()
        self.tracks = [CseqTrack(self, i) for i in range(16)]
        self.active = []
        self.voices = [Voice() for _ in range(self.NUM_VOICES)]
        self.unapplied = {}

    def note_unapplied(self, name):
        self.unapplied[name] = self.unapplied.get(name, 0) + 1

    def setup(self, start_offset):
        t = self.tracks[0]
        t.init(start_offset)
        self.active = [0]

    def open_track(self, num, pos):
        if 0 < num < 16 and num not in self.active:
            self.tracks[num].init(pos)
            self.active.append(num)

    def voice_alloc(self, priority):
        cur = None
        for v in self.voices:
            if cur is None or (v.prio, v.vol) < (cur.prio, cur.vol):
                cur = v
        if cur is None or priority < cur.prio:
            return None
        cur.noteLength = -1
        cur.vol = 1 << 30
        return cur

    def mark_loop(self, start, end):
        if self.loop_start_sample is None:
            self.loop_start_sample = start
            self.loop_end_sample = end
        elif end > (self.loop_end_sample or 0):
            self.loop_end_sample = end

    def tick_threshold(self):
        return 240.0 * 48.0 / self.timebase

    def run(self):
        thr = self.tick_threshold()
        while self.tempoCount >= thr:
            self.tempoCount -= thr
            self.ticked = True
            i = 0
            while i < len(self.active):
                self.tracks[self.active[i]].run()
                i += 1
        self.tempoCount += self.tempo

    def timer(self):
        for v in self.voices:
            self.update_voice(v)
        self.run()

    def all_tracks_ended(self):
        return all(self.tracks[i].state[TS_END] or self.tracks[i].stuck
                   for i in self.active)

    def any_voice_active(self):
        return any(v.state != CS_NONE for v in self.voices)

    def update_voice(self, v):
        st = v.state
        if st == CS_NONE:
            return
        if st == CS_START:
            v.ampl = AMPL_THRESHOLD
            v.state = st = CS_ATTACK
        if st == CS_ATTACK:
            newAmpl = v.ampl
            oldAmpl = v.ampl >> 7
            while True:
                newAmpl = cdiv(newAmpl * v.attackLvl, 256)
                if (newAmpl >> 7) != oldAmpl:
                    break
            v.ampl = newAmpl
            if not v.ampl:
                v.state = CS_DECAY
        elif st == CS_DECAY:
            v.ampl -= v.decayRate
            sustLvl = cnv_sust(v.sustainLvl) << 7
            if v.ampl <= sustLvl:
                v.ampl = sustLvl
                v.state = CS_SUSTAIN
        elif st == CS_RELEASE:
            v.ampl -= v.releaseRate
            if v.ampl <= AMPL_THRESHOLD:
                v.kill()
                return

        modParam = 0
        bMod = bool(v.modDepth)
        if bMod and v.modDelayCnt < v.modDelay:
            v.modDelayCnt += 1
            bMod = False
        if bMod:
            modParam = cnv_sine(v.modCounter >> 8) * v.modRange * v.modDepth
            if v.modType == 1:
                modParam = (modParam * 60) >> 14
            else:
                modParam >>= 8
            counter = v.modCounter + (v.modSpeed << 6)
            while counter >= 0x8000:
                counter -= 0x8000
            v.modCounter = counter

        # pitch in 1/64-semitone units
        totalAdj = v.ext_tune
        if bMod and v.modType == 0:
            totalAdj += modParam
        if v.sweepPitch and v.sweepLen and v.sweepCnt <= v.sweepLen:
            totalAdj += cdiv(v.sweepPitch * (v.sweepLen - v.sweepCnt), v.sweepLen)
            if not v.manualSweep:
                v.sweepCnt += 1
        ratio = math.pow(2.0, totalAdj / 768.0) if totalAdj else 1.0
        v.inc = v.base_rate * v.pitch_mul * ratio / self.rate

        # volume in continuous dB
        totalVol = (v.ampl >> 7) + v.ext_ampl + v.velocity + v.region_vol
        if bMod and v.modType == 1:
            totalVol += modParam
        totalVol = max(-AMPL_K, min(0, totalVol))
        amp = math.pow(10.0, totalVol / 160.0)
        v.vol = int(amp * (1 << 20))
        # pan law: center 64 is full both sides, off-center attenuates the far side
        pan = v.pan + v.ext_pan
        if bMod and v.modType == 2:
            pan += modParam
        pan = max(0, min(127, pan + 64))
        v.vol_l = amp if pan <= 64 else amp * (127 - pan) / 63.0
        v.vol_r = amp if pan >= 64 else amp * pan / 64.0

    def generate(self, n):
        """Mix n samples from all active voices, splitting over aux buses when effects are set."""
        fx = self.effects
        out_l = [0.0] * n
        out_r = [0.0] * n
        aux = {k: ([0.0] * n, [0.0] * n) for k in fx}
        for v in self.voices:
            if v.state == CS_NONE or v.inc <= 0:
                continue
            # an endless note plays one pass of its looped sample, then stops
            cut_at_wrap = v.noteLength <= 0 and CS_ATTACK <= v.state <= CS_SUSTAIN
            smp = v.samples
            ln = len(smp)
            pos = v.pos
            inc = v.inc
            ls = v.loop_start
            vl = v.vol_l
            vr = v.vol_r
            if fx:
                gm = v.send_main
                targets = [(out_l, out_r, vl * gm, vr * gm)]
                if 'a' in fx and v.send_a > 1e-5:
                    al, ar = aux['a']
                    targets.append((al, ar, vl * v.send_a, vr * v.send_a))
                if 'b' in fx and v.send_b > 1e-5:
                    bl, br = aux['b']
                    targets.append((bl, br, vl * v.send_b, vr * v.send_b))
            else:
                targets = [(out_l, out_r, vl, vr)]
            for i in range(n):
                if pos >= ln:
                    if v.loop and not cut_at_wrap:
                        pos = ls + (pos - ln)
                        if pos >= ln:
                            pos = ls
                    else:
                        v.dead_wrap = True
                        break
                ip = int(pos)
                frac = pos - ip
                s0 = smp[ip]
                s1 = smp[ip + 1] if ip + 1 < ln else (smp[ls] if v.loop else 0)
                s = s0 + (s1 - s0) * frac
                for tl, tr, gl, gr in targets:
                    tl[i] += s * gl
                    tr[i] += s * gr
                pos += inc
            v.pos = pos
            if v.dead_wrap:
                v.kill()
        for k, eff in fx.items():
            al, ar = aux[k]
            wl, wr = eff.process(al, ar)
            ret = eff.ret
            for i in range(n):
                out_l[i] += wl[i] * ret
                out_r[i] += wr[i] * ret
        self.now_sample += n
        return out_l, out_r

class CseqTrack:
    """One CSEQ track."""
    def __init__(self, ply, num):
        self.ply = ply
        self.num = num
        self.alive = False
        self.state = [False] * 4

    def init(self, pos):
        self.alive = True
        self.state = [False] * 4
        self.state[TS_NOTEWAIT] = True
        self.pos = pos
        self.wait = 0
        self.stack = []
        self.loopCount = {}
        self.prio = self.ply.channel_prio + 64
        self.patch = 0
        self.bank_no = 0
        self.vol = self.expr = 127
        self.pan = 0
        self.pitchBend = 0
        self.pitchBendRange = 2
        self.transpose = 0
        self.portaKey = 60
        self.portaTime = 0
        self.sweepPitch = 0
        self.a = self.d = self.s = self.r = 0xFF
        self.modType = 0
        self.modDepth = 0
        self.modSpeed = 16
        self.modRange = 1
        self.modDelay = 0
        self.mainsend = 127
        self.fxsend_a = 0
        self.fxsend_b = 0
        self.lastCmp = True
        self.waitChn = False
        self.stuck = False
        self.visited = {}
        self.passes_left = self.ply.loop_passes - 1
        self.over_active = False
        self.over_cmd = 0
        self.over_sub = None
        self.over_value = 0
        self.over_extra = 0
        self.pending_skip = None

    def read8(self):
        v = self.ply.blob[self.pos]
        self.pos += 1
        return v

    def read16(self):
        b = self.ply.blob
        v = (b[self.pos] << 8) | b[self.pos + 1]
        self.pos += 2
        return v

    def read24(self):
        b = self.ply.blob
        v = (b[self.pos] << 16) | (b[self.pos + 1] << 8) | b[self.pos + 2]
        self.pos += 3
        return v

    def readvl_(self):
        v, self.pos = readvl(self.ply.blob, self.pos)
        return v

    def oval8(self, extra=False):
        if self.over_active:
            return self.over_extra if extra else self.over_value
        return self.read8()

    def oval16(self):
        if self.over_active:
            return self.over_value
        return self.read16()

    def ovalvl(self):
        if self.over_active:
            return self.over_value
        return self.readvl_()

    def note_on(self, key, vel, length, gate=None):
        ply = self.ply
        found = ply.bank_lookup(self.bank_no, self.patch, key, vel)
        if found is None:
            return None
        region, cwav = found
        v = ply.voice_alloc(self.prio)
        if v is None:
            return None
        v.state = CS_START
        v.trackId = self.num
        v.flags = None
        v.prio = self.prio
        v.key = key
        v.org_key = region.org_key
        v.velocity = cnv_sust(vel)
        v.region_vol = cnv_sust(region.volume)
        v.pan = region.pan - 64 # region pan is 0..127, center 64
        v.modDelayCnt = 0
        v.modCounter = 0
        v.noteLength = length
        v.samples = cwav.samples
        v.base_rate = cwav.rate
        v.pitch_mul = region.pitch
        v.loop = cwav.loop
        v.loop_start = cwav.loop_start
        v.pos = 0.0
        v.inc = 0.0
        v.dead_wrap = False
        v.attackLvl = cnv_attack(region.attack if self.a == 0xFF else self.a)
        v.decayRate = cnv_fall(region.decay if self.d == 0xFF else self.d)
        v.sustainLvl = region.sustain if self.s == 0xFF else self.s
        v.releaseRate = cnv_fall(region.release if self.r == 0xFF else self.r)
        self.apply_to_voice(v)
        self.update_porta(v, length if gate is None else gate)
        self.portaKey = key
        return v

    def note_on_tie(self, key, vel, gate):
        chn = None
        for v in self.ply.voices:
            if v.state > CS_NONE and v.trackId == self.num and v.state != CS_RELEASE:
                chn = v
                break
        if chn is None:
            return self.note_on(key, vel, -1, gate=gate)
        chn.prio = self.prio
        chn.key = key
        chn.velocity = cnv_sust(vel)
        chn.modDelayCnt = 0
        chn.modCounter = 0
        self.apply_to_voice(chn)
        self.update_porta(chn, gate)
        self.portaKey = key
        return chn

    def apply_to_voice(self, v):
        ply = self.ply
        final = ply.masterVol + ply.baseVol + cnv_sust(self.vol) + cnv_sust(self.expr)
        v.ext_ampl = max(-AMPL_K, final)
        v.ext_pan = self.pan
        v.ext_tune = (v.key - v.org_key) * 64 + ((self.pitchBend * self.pitchBendRange) >> 1)
        v.modType = self.modType
        v.modSpeed = self.modSpeed
        v.modDepth = self.modDepth
        v.modRange = self.modRange
        v.modDelay = self.modDelay
        # bus send gains, applied only when effects are configured
        v.send_main = 10.0 ** (cnv_sust(self.mainsend) / 160.0)
        v.send_a = 10.0 ** (cnv_sust(self.fxsend_a) / 160.0)
        v.send_b = 10.0 ** (cnv_sust(self.fxsend_b) / 160.0)

    def update_porta(self, v, gate):
        v.manualSweep = False
        v.sweepPitch = self.sweepPitch
        v.sweepCnt = 0
        if not self.state[TS_PORTA]:
            v.sweepLen = 0
            return
        diff = (self.portaKey - v.key) << 22
        v.sweepPitch += diff >> 16
        if not self.portaTime:
            v.sweepLen = v.noteLength if gate is None else gate
            v.manualSweep = True
        else:
            sq = self.portaTime * self.portaTime
            v.sweepLen = (abs(v.sweepPitch) * sq) >> 11

    def release_all(self):
        for v in self.ply.voices:
            if v.state > CS_NONE and v.trackId == self.num and v.state != CS_RELEASE:
                v.release()

    def tick_lengths(self):
        for v in self.ply.voices:
            if v.trackId != self.num:
                continue
            if v.state > CS_START:
                if v.state < CS_RELEASE and v.noteLength > 0:
                    v.noteLength -= 1
                    if not v.noteLength:
                        v.release()
                if v.manualSweep and v.sweepCnt < v.sweepLen:
                    v.sweepCnt += 1

    def push_params(self):
        for v in self.ply.voices:
            if v.state > CS_NONE and v.trackId == self.num:
                self.apply_to_voice(v)

    def run(self):
        ply = self.ply
        self.tick_lengths()
        if self.state[TS_END]:
            return
        if self.waitChn:
            for v in ply.voices:
                if v.state != CS_NONE and v.trackId == self.num:
                    return
            self.waitChn = False
        if self.wait < 0:
            # uninitialized runtime length = mark idle
            self.stuck = True
            return
        if self.wait:
            self.wait -= 1
            if self.wait:
                return
        blob = ply.blob
        guard = 0
        while not self.wait:
            guard += 1
            if guard > 100000 or self.pos < 0 or self.pos >= len(blob):
                self.state[TS_END] = True
                return
            if self.pending_skip and self.pos == self.pending_skip[1]:
                # ramp payload of a time prefix, after its inner command ran
                self.pos += self.pending_skip[0]
                self.pending_skip = None
            if self.over_active:
                cmd = self.over_cmd
            else:
                if self.pos not in self.visited:
                    self.visited[self.pos] = ply.now_sample
                cmd = self.read8()

            if cmd < 0x80:
                key = (cmd + self.transpose) & 0xFF
                vel = self.oval8(extra=True)
                length = self.ovalvl()
                if self.state[TS_NOTEWAIT]:
                    self.wait = length
                if self.state[TS_TIE]:
                    v = self.note_on_tie(key, vel, length)
                else:
                    v = self.note_on(key, vel, length)
                if self.state[TS_NOTEWAIT] and length == 0 and v is not None:
                    self.waitChn = True
                    if cmd not in (0xA0, 0xA1):
                        self.over_active = False
                    return
            elif cmd == 0x80:
                self.wait = self.ovalvl()
            elif cmd == 0x81:
                prg = self.ovalvl()
                self.patch = prg & 0x7F
                if prg >> 8:
                    self.bank_no = (prg >> 8) & 3
            elif cmd == 0x88:
                tnum = self.read8()
                dest = self.read24()
                ply.open_track(tnum, dest)
            elif cmd == 0x89: # jump
                dest = self.read24()
                if dest in self.visited:
                    ply.loop_detected = True
                    ply.mark_loop(self.visited[dest], ply.now_sample)
                    if self.passes_left > 0:
                        self.passes_left -= 1
                        self.visited = {}
                        self.pos = dest
                    else:
                        self.state[TS_END] = True
                        return
                else:
                    self.pos = dest
            elif cmd == 0x8A: # call
                dest = self.read24()
                if len(self.stack) < 3:
                    self.stack.append(('call', self.pos))
                    self.pos = dest
            elif cmd == 0xFD: # return
                if self.stack and self.stack[-1][0] == 'call':
                    self.pos = self.stack.pop()[1]
            elif cmd == 0xFF:
                self.state[TS_END] = True
                return
            elif cmd == 0xFE:
                self.read16() # alloc track mask
            elif cmd == 0xB0:
                ply.timebase = self.oval8() or 48
            elif cmd == 0xD4: # loop start
                count = self.oval8()
                if len(self.stack) < 3:
                    self.stack.append(('loop', self.pos, ply.now_sample, count))
            elif cmd == 0xFC: # loop end
                if self.stack and self.stack[-1][0] == 'loop':
                    kind, rpos, sample0, count = self.stack[-1]
                    if not count:
                        ply.loop_detected = True
                        ply.mark_loop(sample0, ply.now_sample)
                        if self.passes_left > 0:
                            self.passes_left -= 1
                            self.stack[-1] = (kind, rpos, ply.now_sample, count)
                            self.pos = rpos
                        else:
                            self.stack.pop()
                    else:
                        count -= 1
                        if count:
                            self.stack[-1] = (kind, rpos, sample0, count)
                            self.pos = rpos
                        else:
                            self.stack.pop()
            elif cmd == 0xC0:
                self.pan = self.oval8() - 64
                self.push_params()
            elif cmd == 0xC1:
                self.vol = self.oval8()
                self.push_params()
            elif cmd == 0xD5:
                self.expr = self.oval8()
                self.push_params()
            elif cmd == 0xC2:
                ply.masterVol = cnv_sust(self.oval8())
                for i in ply.active:
                    ply.tracks[i].push_params()
            elif cmd == 0xC3:
                self.transpose = s8(self.oval8())
            elif cmd == 0xC4:
                self.pitchBend = s8(self.oval8())
                self.push_params()
            elif cmd == 0xC5:
                self.pitchBendRange = self.read8()
                self.push_params()
            elif cmd == 0xC6:
                # priority = player base + track value, same allocation formula as the DS driver
                self.prio = (ply.channel_prio + self.read8()) & 0xFF
            elif cmd == 0xC7:
                self.state[TS_NOTEWAIT] = bool(self.read8())
            elif cmd == 0xC8:
                self.state[TS_TIE] = bool(self.read8())
                self.release_all()
            elif cmd == 0xC9:
                self.portaKey = (self.read8() + self.transpose) & 0xFF
                self.state[TS_PORTA] = True
            elif cmd == 0xCE:
                self.state[TS_PORTA] = bool(self.read8())
            elif cmd == 0xCF:
                self.portaTime = self.oval8()
            elif cmd == 0xE3:
                self.sweepPitch = s16(self.oval16())
                self.state[TS_PORTA] = True
            elif cmd == 0xCA:
                self.modDepth = self.oval8()
                self.push_params()
            elif cmd == 0xCB:
                self.modSpeed = self.oval8()
                self.push_params()
            elif cmd == 0xCC:
                self.modType = self.read8()
                self.push_params()
            elif cmd == 0xCD:
                self.modRange = self.read8()
                self.push_params()
            elif cmd == 0xE0:
                self.modDelay = self.oval16()
                self.push_params()
            elif cmd == 0xE1:
                ply.tempo = self.oval16()
            elif cmd == 0xD0:
                self.a = self.oval8()
            elif cmd == 0xD1:
                self.d = self.oval8()
            elif cmd == 0xD2:
                self.s = self.oval8()
            elif cmd == 0xD3:
                self.r = self.oval8()
            elif cmd == 0xB6:
                self.bank_no = self.oval8() & 3
            elif cmd in (0xD9, 0xDA, 0xDB): # fxsend_a / fxsend_b / mainsend
                val = self.oval8()
                if ply.effects:
                    if cmd == 0xD9:
                        self.fxsend_a = val
                    elif cmd == 0xDA:
                        self.fxsend_b = val
                    else:
                        self.mainsend = val
                    self.push_params()
                else:
                    ply.note_unapplied(CSEQ_NAMES[cmd])
            elif cmd in (0xA0, 0xA1): # random / variable prefixes
                self.over_cmd = self.read8()
                self.over_sub = None
                if self.over_cmd < 0x80 or self.over_cmd == 0x88:
                    self.over_extra = self.read8()
                elif self.over_cmd == 0xF0:
                    self.over_sub = self.read8()
                    if 0x80 <= self.over_sub <= 0x95:
                        self.over_extra = self.read8() # variable index
                if cmd == 0xA0:
                    lo = s16(self.read16())
                    hi = s16(self.read16())
                    span = hi - lo + 1
                    if span == 0:
                        # empty range (max == min-1): div-by-zero on hardware, pick min
                        self.ply.note_unapplied('_r_span0')
                        self.over_value = lo
                    else:
                        # C-truncated modulo, not Python %, differs when max < min
                        r = self.ply.rng.calc()
                        self.over_value = (r - cdiv(r, span) * span) + lo
                else:
                    self.over_value = self.ply.variables[self.read8() & 0x1F]
                self.over_active = True
                continue
            elif cmd == 0xA2: # if
                if not self.lastCmp:
                    self.pos = skip_command(blob, self.pos)
            elif cmd in (0xA3, 0xA4, 0xA5): # time prefixes
                # ramp prefixes: apply the inner command instantly, drop the ramp payload
                self.ply.note_unapplied(CSEQ_NAMES[cmd])
                inner_end = skip_command(blob, self.pos)
                self.pending_skip = ({0xA3: 2, 0xA4: 4, 0xA5: 1}[cmd], inner_end)
                continue
            elif cmd == 0xF0: # extended
                if self.over_active:
                    sub = self.over_sub
                else:
                    sub = self.read8()
                if not 0x80 <= sub <= 0x95:
                    # mod2-4 / usercall: consume args, count as unapplied
                    if not self.over_active:
                        self.pos += _ext_arg_size(sub)
                    self.ply.note_unapplied('ext_%02x' % sub)
                    if cmd not in (0xA0, 0xA1):
                        self.over_active = False
                    continue
                var_no = (self.over_extra if self.over_active else self.read8()) & 0x1F
                val = s16(self.oval16())
                var = self.ply.variables[var_no]
                if sub == 0x80:
                    var = val
                elif sub == 0x81:
                    var = s16(var + val)
                elif sub == 0x82:
                    var = s16(var - val)
                elif sub == 0x83:
                    var = s16(var * val)
                elif sub == 0x84:
                    var = s16(cdiv(var, val)) if val else var
                elif sub == 0x85:
                    var = s16(var >> -val) if val < 0 else s16(var << val)
                elif sub == 0x86:
                    r = self.ply.rng.calc()
                    var = -(r % (-val + 1)) if val < 0 else (r % (val + 1))
                elif sub == 0x87:
                    var = s16(var & val)
                elif sub == 0x88:
                    var = s16(var | val)
                elif sub == 0x89:
                    var = s16(var ^ val)
                elif sub == 0x8A:
                    var = s16(~val)
                elif sub == 0x8B:
                    var = s16(var % val) if val else var
                elif 0x90 <= sub <= 0x95:
                    self.lastCmp = (var == val if sub == 0x90 else
                        var >= val if sub == 0x91 else
                        var > val if sub == 0x92 else
                        var <= val if sub == 0x93 else
                        var < val if sub == 0x94 else
                        var != val)
                    var = None
                else:
                    self.ply.note_unapplied('ext_%02x' % sub)
                    var = None
                if var is not None and 0x80 <= sub <= 0x8B:
                    self.ply.variables[var_no] = var
            else:
                arg = CSEQ_ARGS.get(cmd)
                if arg is None:
                    self.state[TS_END] = True
                    return
                if arg == ARG_U8:
                    self.oval8()
                elif arg in (ARG_S16, ARG_U16):
                    self.oval16()
                elif arg == ARG_VL:
                    self.ovalvl()
                elif arg == ARG_U24:
                    self.read24()
                elif arg == ARG_U8_U24:
                    self.read8()
                    self.read24()
                self.ply.note_unapplied(CSEQ_NAMES.get(cmd, '?%02x' % cmd))

            if cmd not in (0xA0, 0xA1):
                self.over_active = False
