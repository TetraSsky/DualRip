# Part of DualRip. Core playback logic is a faithful Python port of the FeOS
# Sound System (fincs), as adapted by Naram Qashat (CyberBotX) for the NCSF
# player (github.com/CyberBotX/in_xsf, src/in_ncsf/SSEQPlayer). Lookup tables
# come from disassembly of Nintendo's NNS sound driver by those authors.
# FIDELITY-CRITICAL: C integer semantics (truncating division, arithmetic
# shifts, table indexing) are intentional. Do not "simplify".

import numpy as np

from ..cprims import (
    AMPL_K,
    AMPL_THRESHOLD,
    ARM7_CLOCK,
    CS_ATTACK,
    CS_DECAY,
    CS_NONE,
    CS_RELEASE,
    CS_START,
    CS_SUSTAIN,
    F_UPDPAN,
    F_UPDTMR,
    F_UPDVOL,
    TF_LEN,
    TF_MOD,
    TF_PAN,
    TF_TIMER,
    TF_VOL,
    TS_PORTA,
    calc_voldiv_shift,
    cdiv,
    cnv_sine,
    cnv_sust,
    timer_adjust,
)
from ..tables import GETVOLTBL, WAVEDUTYTBL


class Reg:
    __slots__ = (
        'volumeMul',
        'volumeDiv',
        'panning',
        'waveDuty',
        'repeatMode',
        'format',
        'enable',
        'source',
        'timer',
        'psgX',
        'psgLast',
        'psgLastCount',
        'samplePosition',
        'sampleIncrease',
        'loopStart',
        'length',
        'totalLength',
    )

    def __init__(self):
        self.clear_cr()
        self.source = None
        self.timer = 0
        self.psgX = 0
        self.psgLast = 0
        self.psgLastCount = 0
        self.samplePosition = 0.0
        self.sampleIncrease = 0.0
        self.loopStart = 0
        self.length = 0
        self.totalLength = 0

    def clear_cr(self):
        self.volumeMul = self.volumeDiv = self.panning = 0
        self.waveDuty = self.repeatMode = self.format = 0
        self.enable = False

    def set_cr(self, cr):
        self.volumeMul = cr & 0x7F
        self.volumeDiv = (cr >> 8) & 0x03
        self.panning = (cr >> 16) & 0x7F
        self.waveDuty = (cr >> 24) & 0x07
        self.repeatMode = (cr >> 27) & 0x03
        self.format = (cr >> 29) & 0x03
        self.enable = bool((cr >> 31) & 0x01)


class Channel:
    def __init__(self, chn_id, player):
        self.chnId = chn_id
        self.ply = player
        self.reg = Reg()
        self.tempCR = 0
        self.tempSOURCE = None
        self.tempTIMER = 0
        self.tempREPEAT = 0
        self.tempLENGTH = 0
        self.state = CS_NONE
        self.trackId = -1
        self.prio = 0
        self.manualSweep = False
        self.flags = [False, False, False]
        self.pan = 0
        self.extAmpl = 0
        self.velocity = 0
        self.extPan = 0
        self.key = 0
        self.ampl = 0
        self.extTune = 0
        self.orgKey = 0
        self.modType = 0
        self.modSpeed = 0
        self.modDepth = 0
        self.modRange = 0
        self.modDelay = 0
        self.modDelayCnt = 0
        self.modCounter = 0
        self.sweepLen = 0
        self.sweepCnt = 0
        self.sweepPitch = 0
        self.attackLvl = 0
        self.sustainLvl = 0x7F
        self.decayRate = 0
        self.releaseRate = 0xFFFF
        self.noteLength = -1
        self.vol = 0
        self.kill_after_block = False
        self.cut_at_wrap = False
        self.loop_pass_sample = None

    # --- FSS Chn_UpdateVol
    def update_vol(self, trk):
        final = self.ply.masterVol + self.ply.sseqVol
        final += cnv_sust(trk.vol) + cnv_sust(trk.expr)
        if final < -AMPL_K:
            final = -AMPL_K
        self.extAmpl = final

    def update_pan(self, trk):
        self.extPan = trk.pan

    def update_tune(self, trk):
        tune = (self.key - self.orgKey) * 64
        tune += (trk.pitchBend * trk.pitchBendRange) >> 1
        self.extTune = tune

    def update_mod(self, trk):
        self.modType = trk.modType
        self.modSpeed = trk.modSpeed
        self.modDepth = trk.modDepth
        self.modRange = trk.modRange
        self.modDelay = trk.modDelay

    def update_porta(self, trk, gate=None):
        # `gate` is the note event's written length in ticks. For normal notes
        # it equals noteLength; for tie-mode notes the channel is endless
        # (noteLength -1) but Nintendo's driver still sweeps the portamento
        # over the written note length. (FSS/NCSF drop the sweep in that case,
        # which silences the pitch glide some games apply to tied notes.)
        self.manualSweep = False
        self.sweepPitch = trk.sweepPitch
        self.sweepCnt = 0
        if not trk.state[TS_PORTA]:
            self.sweepLen = 0
            return
        diff = (trk.portaKey - self.key) << 22
        self.sweepPitch += diff >> 16
        if not trk.portaTime:
            self.sweepLen = self.noteLength if gate is None else gate
            self.manualSweep = True
        else:
            sq_time = trk.portaTime * trk.portaTime
            self.sweepLen = (abs(self.sweepPitch) * sq_time) >> 11

    def release(self):
        self.noteLength = -1
        self.prio = 1
        self.state = CS_RELEASE

    def kill(self):
        self.state = CS_NONE
        self.trackId = -1
        self.prio = 0
        self.reg.clear_cr()
        self.vol = 0
        self.noteLength = -1

    # --- FSS Chn_UpdateTracks
    def update_track(self):
        if self.trackId == -1:
            return
        trk = self.ply.tracks[self.trackId]
        tf = trk.updateFlags
        if not any(tf):
            return
        if tf[TF_LEN]:
            st = self.state
            if st > CS_START:
                if st < CS_RELEASE:
                    self.noteLength -= 1
                    if not self.noteLength:
                        self.release()
                if self.manualSweep and self.sweepCnt < self.sweepLen:
                    self.sweepCnt += 1
        if tf[TF_VOL]:
            self.update_vol(trk)
            self.flags[F_UPDVOL] = True
        if tf[TF_PAN]:
            self.update_pan(trk)
            self.flags[F_UPDPAN] = True
        if tf[TF_TIMER]:
            self.update_tune(trk)
            self.flags[F_UPDTMR] = True
        if tf[TF_MOD]:
            old = self.modType
            new = trk.modType
            self.update_mod(trk)
            if old != new:
                for t in (old, new):
                    self.flags[F_UPDTMR if t == 0 else (F_UPDPAN if t == 2 else F_UPDVOL)] = True

    # --- FSS Snd_UpdChannel
    def update(self):
        if self.state > CS_START and not self.reg.enable:
            self.kill()
            return
        bNotInSustain = self.state != CS_SUSTAIN
        bInStart = self.state == CS_START
        bPitchSweep = (
            bool(self.sweepPitch) and bool(self.sweepLen) and self.sweepCnt <= self.sweepLen
        )
        bModulation = bool(self.modDepth)
        bVolNeedUpdate = self.flags[F_UPDVOL] or bNotInSustain
        bPanNeedUpdate = self.flags[F_UPDPAN] or bInStart
        bTmrNeedUpdate = self.flags[F_UPDTMR] or bInStart or bPitchSweep
        modParam = 0

        st = self.state
        if st == CS_NONE:
            return
        if st == CS_START:
            self.reg.clear_cr()
            self.reg.source = self.tempSOURCE
            self.reg.loopStart = self.tempREPEAT
            self.reg.length = self.tempLENGTH
            self.reg.totalLength = self.reg.loopStart + self.reg.length
            self.ampl = AMPL_THRESHOLD
            self.state = st = CS_ATTACK
        if st == CS_ATTACK:
            newAmpl = self.ampl
            oldAmpl = self.ampl >> 7
            while True:
                newAmpl = cdiv(newAmpl * self.attackLvl, 256)
                if (newAmpl >> 7) != oldAmpl:
                    break
            self.ampl = newAmpl
            if not self.ampl:
                self.state = CS_DECAY
        elif st == CS_DECAY:
            self.ampl -= self.decayRate
            sustLvl = cnv_sust(self.sustainLvl) << 7
            if self.ampl <= sustLvl:
                self.ampl = sustLvl
                self.state = CS_SUSTAIN
        elif st == CS_RELEASE:
            self.ampl -= self.releaseRate
            if self.ampl <= AMPL_THRESHOLD:
                self.kill()
                return

        if bModulation and self.modDelayCnt < self.modDelay:
            self.modDelayCnt += 1
            bModulation = False

        if bModulation:
            if self.modType == 0:
                bTmrNeedUpdate = True
            elif self.modType == 1:
                bVolNeedUpdate = True
            else:
                bPanNeedUpdate = True
            modParam = cnv_sine(self.modCounter >> 8) * self.modRange * self.modDepth
            if self.modType == 1:
                modParam = (modParam * 60) >> 14
            else:
                modParam >>= 8
            counter = self.modCounter + (self.modSpeed << 6)
            while counter >= 0x8000:
                counter -= 0x8000
            self.modCounter = counter

        if bTmrNeedUpdate:
            totalAdj = self.extTune
            if bModulation and not self.modType:
                totalAdj += modParam
            if bPitchSweep:
                ln = self.sweepLen
                cnt = self.sweepCnt
                totalAdj += cdiv(self.sweepPitch * (ln - cnt), ln)
                if not self.manualSweep:
                    self.sweepCnt += 1
            tmr = self.tempTIMER
            if totalAdj:
                tmr = timer_adjust(tmr, totalAdj)
            self.reg.timer = (-tmr) & 0xFFFF
            self.reg.sampleIncrease = (ARM7_CLOCK / (self.ply.sampleRate * 2.0)) / (
                0x10000 - self.reg.timer
            )
            self.flags[F_UPDTMR] = False

        if bVolNeedUpdate or bPanNeedUpdate:
            cr = self.tempCR
            if bVolNeedUpdate:
                totalVol = (self.ampl >> 7) + self.extAmpl + self.velocity
                if bModulation and self.modType == 1:
                    totalVol += modParam
                totalVol += AMPL_K
                totalVol = max(0, min(AMPL_K, totalVol))
                cr &= ~(0x7F | (3 << 8))
                cr |= GETVOLTBL[totalVol]
                if totalVol < AMPL_K - 240:
                    cr |= 3 << 8
                elif totalVol < AMPL_K - 120:
                    cr |= 2 << 8
                elif totalVol < AMPL_K - 60:
                    cr |= 1 << 8
                self.vol = ((cr & 0x7F) << 4) >> calc_voldiv_shift((cr >> 8) & 3)
                self.flags[F_UPDVOL] = False
            if bPanNeedUpdate:
                realPan = self.pan + self.extPan
                if bModulation and self.modType == 2:
                    realPan += modParam
                realPan += 64
                realPan = max(0, min(127, realPan))
                cr &= ~(0x7F << 16)
                cr |= realPan << 16
                self.flags[F_UPDPAN] = False
            self.tempCR = cr
            self.reg.set_cr(cr)

    def generate_block(self, n):
        """Returns (left, right) int64 arrays of n samples, advances channel."""
        reg = self.reg
        inc = reg.sampleIncrease
        pos0 = reg.samplePosition
        if inc <= 0:
            return None
        positions = pos0 + inc * np.arange(n, dtype=np.float64)

        if reg.format != 3:
            src = reg.source
            data = src.data
            totalLen = reg.totalLength
            loopStart = reg.loopStart
            loopLen = reg.length
            idx0 = np.floor(positions).astype(np.int64)
            valid = idx0 >= 0
            if reg.repeatMode == 1 and loopLen > 0:
                big = idx0 >= totalLen
                if big.any():
                    idx0 = np.where(big, loopStart + (idx0 - loopStart) % loopLen, idx0)
                idx1 = idx0 + 1
                big1 = idx1 >= totalLen
                if big1.any():
                    idx1 = np.where(big1, loopStart + (idx1 - loopStart) % loopLen, idx1)
            else:
                valid &= idx0 < totalLen
                idx1 = np.minimum(idx0 + 1, totalLen - 1)
            idx0c = np.clip(idx0, 0, len(data) - 1)
            idx1c = np.clip(idx1, 0, len(data) - 1)
            d0 = data[idx0c]
            d1 = data[idx1c]
            frac = positions - np.floor(positions)
            samples = (d0 + frac * (d1 - d0)).astype(np.int64)
            samples[~valid] = 0
            if reg.repeatMode == 1 and self.loop_pass_sample is None and inc > 0:
                # remember when the playhead first enters the loop region
                if pos0 + inc * n > loopStart:
                    k0 = 0 if pos0 >= loopStart else int(np.ceil((loopStart - pos0) / inc))
                    if k0 < n:
                        self.loop_pass_sample = self.ply.now_sample + k0
            if self.cut_at_wrap and reg.repeatMode == 1:
                past = positions >= totalLen
                if past.any():
                    # raw mode: stop an endless looped note after one full
                    # pass through the sample, and record the loop points
                    samples[past] = 0
                    self.kill_after_block = True
                    self.ply.loop_detected = True
                    k = int(np.argmax(past))
                    start = (
                        self.loop_pass_sample
                        if self.loop_pass_sample is not None
                        else self.ply.now_sample + k
                    )
                    self.ply.mark_loop(start, self.ply.now_sample + k)
            new_pos = pos0 + inc * n
            if reg.repeatMode == 1 and loopLen > 0:
                while new_pos >= totalLen:
                    new_pos -= loopLen
            reg.samplePosition = new_pos
            if reg.repeatMode != 1 and new_pos >= totalLen:
                self.kill_after_block = True
        elif self.chnId < 8:
            samples = np.zeros(n, dtype=np.int64)
            reg.samplePosition = pos0 + inc * n
        elif self.chnId < 14:
            duty = WAVEDUTYTBL[reg.waveDuty * 8 : reg.waveDuty * 8 + 8]
            duty = np.asarray(duty, dtype=np.int64)
            idx = np.floor(positions).astype(np.int64)
            samples = duty[idx & 0x7]
            samples[idx < 0] = 0
            reg.samplePosition = pos0 + inc * n
        else:
            samples = np.zeros(n, dtype=np.int64)
            psgX = reg.psgX
            psgLast = reg.psgLast
            psgLastCount = reg.psgLastCount
            fl = np.floor(positions).astype(np.int64)
            for k in range(n):
                cur = fl[k]
                if cur < 0:
                    continue
                if psgLastCount != cur:
                    for _ in range(cur - psgLastCount):
                        if psgX & 1:
                            psgX = (psgX >> 1) ^ 0x6000
                            psgLast = -0x7FFF
                        else:
                            psgX >>= 1
                            psgLast = 0x7FFF
                    psgLastCount = cur
                samples[k] = psgLast
            reg.psgX = psgX
            reg.psgLast = psgLast
            reg.psgLastCount = psgLastCount
            reg.samplePosition = pos0 + inc * n

        # hardware volume & pan (NCSF GenerateSamples)
        datashift = reg.volumeDiv
        if datashift == 3:
            datashift = 4
        if reg.volumeMul != 127:
            samples = (samples * reg.volumeMul) >> 7
        samples >>= datashift
        panning = reg.panning
        left = samples if panning == 0 else (samples * (127 - panning)) >> 7
        right = samples if panning == 127 else (samples * panning) >> 7
        if panning == 0:
            right = np.zeros_like(samples)
        elif panning == 127:
            left = np.zeros_like(samples)
        return left, right
