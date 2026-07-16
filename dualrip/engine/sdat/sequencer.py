# Part of DualRip. SSEQ bytecode interpreter — faithful port of FeOS Sound
# System (fincs) / CyberBotX/in_xsf, derived from NNS driver disassembly.
# FIDELITY-CRITICAL: C integer semantics, literal dispatches are intentional.

from ..cprims import (
    CS_NONE,
    CS_RELEASE,
    CS_START,
    NOISE_CHN_ORDER,
    PCM_CHN_ORDER,
    PSG_CHN_ORDER,
    SCHANNEL_ENABLE,
    SOUND_FORMAT_PSG,
    SOUND_ONE_SHOT,
    SOUND_REPEAT,
    F_UPDTMR,
    TF_LEN,
    TF_MOD,
    TF_PAN,
    TF_TIMER,
    TF_VOL,
    TS_ALLOC,
    TS_END,
    TS_NOTEWAIT,
    TS_PORTA,
    TS_TIE,
    cdiv,
    cnv_attack,
    cnv_fall,
    cnv_sust,
    s8,
    s16,
)
from .channel import Channel

def readvl(blob, pc):
    """Read variable-length int from SSEQ bytecode, return (value, new_pc)."""
    x = 0
    while True:
        d = blob[pc]
        pc += 1
        x = (x << 7) | (d & 0x7F)
        if not (d & 0x80):
            break
    return x, pc

# SSEQ opcodes exposed for bankmap.py (static patch scanner).
# Inside Track.run() the full set is dispatched on literal values with
# naming comments — intentionally mirrors the C++ reference line-by-line.
SSEQ_NOTE_LIMIT = 0x80 # command values below this are note-on events
SSEQ_CMD_PATCH = 0x81
SSEQ_CMD_OPEN_TRACK = 0x93
SSEQ_CMD_GOTO = 0x94
SSEQ_CMD_CALL = 0x95
SSEQ_CMD_RANDOM = 0xA0
SSEQ_CMD_FROM_VAR = 0xA1
SSEQ_VAR_CMD_FIRST = 0xB0 # variable ops / comparisons take a 1-byte
SSEQ_VAR_CMD_LAST = 0xBD # argument in Random/FromVariable override mode
SSEQ_CMD_FIN = 0xFF

VARIABLE_BYTE_COUNT = 1 << 7
EXTRA_BYTE = 1 << 6
_CMD_BYTES = {}
for c in (0x80, 0x81):
    _CMD_BYTES[c] = VARIABLE_BYTE_COUNT
for c in (
    0xC0, 0xC1, 0xC2, 0xC6, 0xC7, 0xC8, 0xD5, 0xD4,
    0xC3, 0xC4, 0xC5, 0xD0, 0xD1, 0xD2, 0xD3, 0xC9,
    0xCE, 0xCF, 0xCA, 0xCB, 0xCC, 0xCD, 0xD6, 0xD7,
):
    _CMD_BYTES[c] = 1
for c in (0xFE, 0xE1, 0xE3, 0xE0):
    _CMD_BYTES[c] = 2
for c in (
    0x94, 0x95, 0xB0, 0xB1, 0xB2, 0xB3, 0xB4, 0xB5,
    0xB6, 0xB8, 0xB9, 0xBA, 0xBB, 0xBC, 0xBD,
):
    _CMD_BYTES[c] = 3
_CMD_BYTES[0x93] = 4
_CMD_BYTES[0xA1] = 1 | EXTRA_BYTE
_CMD_BYTES[0xA0] = 4 | EXTRA_BYTE

def sseq_command_byte_count(cmd):
    if cmd < 0x80:
        return 1 | VARIABLE_BYTE_COUNT
    return _CMD_BYTES.get(cmd, 0)

class Rng:
    """LCG random number generator (seed 0x12345678), matches NNS driver."""
    def __init__(self):
        self.u = 0x12345678

    def calc(self):
        self.u = (self.u * 1664525 + 1013904223) & 0xFFFFFFFF
        return (self.u >> 16) & 0xFFFF

class Track:
    """One SSEQ track: bytecode pointer, state, note-on helpers."""
    __slots__ = (
        'trackId',
        'state',
        'num',
        'prio',
        'ply',
        'startPos',
        'pos',
        'stack',
        'stackPos',
        'loopCount',
        'over_active',
        'over_cmd',
        'over_value',
        'over_extra',
        'lastComparisonResult',
        'wait',
        'patch',
        'portaKey',
        'portaTime',
        'sweepPitch',
        'vol',
        'expr',
        'pan',
        'pitchBendRange',
        'pitchBend',
        'transpose',
        'a',
        'd',
        's',
        'r',
        'modType',
        'modSpeed',
        'modDepth',
        'modRange',
        'modDelay',
        'updateFlags',
        'waitChn',
        'visited',
        'passes_left',
    )

    def __init__(self):
        self.zero()

    def zero(self):
        self.trackId = -1
        self.state = [False] * 5
        self.num = self.prio = 0
        self.ply = None
        self.startPos = self.pos = 0
        self.stack = [None, None, None]
        self.stackPos = 0
        self.loopCount = [0, 0, 0]
        self.over_active = False
        self.over_cmd = 0
        self.over_value = 0
        self.over_extra = 0
        self.lastComparisonResult = True
        self.wait = 0
        self.patch = 0
        self.portaKey = self.portaTime = 0
        self.sweepPitch = 0
        self.vol = self.expr = 0
        self.pan = 0
        self.pitchBendRange = 0
        self.pitchBend = self.transpose = 0
        self.a = self.d = self.s = self.r = 0
        self.modType = self.modSpeed = self.modDepth = self.modRange = 0
        self.modDelay = 0
        self.updateFlags = [False] * 5
        self.waitChn = False
        self.visited = {}
        self.passes_left = 0

    def init(self, handle, player, pos, n):
        self.trackId = handle
        self.num = n
        self.ply = player
        self.startPos = pos
        self.clear_state()

    def clear_state(self):
        self.state = [False] * 5
        self.state[TS_ALLOC] = True
        self.state[TS_NOTEWAIT] = True
        self.prio = self.ply.prio + 64
        self.pos = self.startPos
        self.stackPos = 0
        self.wait = 0
        self.patch = 0
        self.portaKey = 60
        self.portaTime = 0
        self.sweepPitch = 0
        self.vol = self.expr = 127
        self.pan = 0
        self.pitchBendRange = 2
        self.pitchBend = self.transpose = 0
        self.a = self.d = self.s = self.r = 0xFF
        self.modType = 0
        self.modRange = 1
        self.modSpeed = 16
        self.modDelay = 0
        self.modDepth = 0
        self.waitChn = False
        self.visited = {}
        self.passes_left = self.ply.loop_passes - 1

    # override helpers
    def oval_read8(self, extra=False):
        blob = self.ply.blob
        if self.over_active:
            return self.over_extra if extra else self.over_value
        v = blob[self.pos]
        self.pos += 1
        return v

    def oval_read16(self):
        if self.over_active:
            return self.over_value
        blob = self.ply.blob
        v = blob[self.pos] | (blob[self.pos + 1] << 8)
        self.pos += 2
        return v

    def oval_readvl(self):
        if self.over_active:
            return self.over_value
        v, self.pos = readvl(self.ply.blob, self.pos)
        return v

    def read8(self):
        v = self.ply.blob[self.pos]
        self.pos += 1
        return v

    def read16(self):
        blob = self.ply.blob
        v = blob[self.pos] | (blob[self.pos + 1] << 8)
        self.pos += 2
        return v

    def read24(self):
        blob = self.ply.blob
        v = blob[self.pos] | (blob[self.pos + 1] << 8) | (blob[self.pos + 2] << 16)
        self.pos += 3
        return v

    # --- FSS Note_On --------------------------------------------------
    def note_on(self, key, vel, length, gate=None):
        ply = self.ply
        bank = ply.bank
        if self.patch >= len(bank):
            return -1
        entry = bank[self.patch]
        noteDef = None
        fRecord = entry.record
        if fRecord == 16:
            if not (
                entry.instruments
                and entry.instruments[0].lowNote <= key <= entry.instruments[-1].highNote
            ):
                return -1
            noteDef = entry.instruments[key - entry.instruments[0].lowNote]
            fRecord = noteDef.record
        elif fRecord == 17:
            noteDef = None
            for inst in entry.instruments:
                if key <= inst.highNote:
                    noteDef = inst
                    break
            if noteDef is None:
                return -1
            fRecord = noteDef.record

        chn = None
        nCh = -1
        bIsPCM = True
        if not fRecord:
            return -1
        elif fRecord == 1:
            if noteDef is None:
                noteDef = entry.instruments[0]
        elif fRecord < 4:
            bIsPCM = False
            if noteDef is None:
                noteDef = entry.instruments[0]
            if fRecord == 3:
                nCh = ply.channel_alloc(2, self.prio)
                if nCh < 0:
                    return -1
                chn = ply.channels[nCh]
                chn.tempCR = SOUND_FORMAT_PSG | SCHANNEL_ENABLE
            else:
                nCh = ply.channel_alloc(1, self.prio)
                if nCh < 0:
                    return -1
                chn = ply.channels[nCh]
                chn.tempCR = SOUND_FORMAT_PSG | SCHANNEL_ENABLE | ((noteDef.swav & 0x7) << 24)
            chn.tempTIMER = 0x1000000 // (262 * 8) # key #60 (C4)
            chn.reg.samplePosition = -1.0
            chn.reg.psgX = 0x7FFF
            chn.reg.psgLast = 0
            chn.reg.psgLastCount = 0
        else:
            return -1

        if bIsPCM:
            swar = ply.waveArc[noteDef.swar] if noteDef.swar < len(ply.waveArc) else None
            if swar is None or noteDef.swav >= len(swar):
                return -1
            swav = swar[noteDef.swav]
            nCh = ply.channel_alloc(0, self.prio)
            if nCh < 0:
                return -1
            chn = ply.channels[nCh]
            chn.tempCR = (
                ((swav.waveType & 3) << 29)
                | (SOUND_REPEAT if swav.loop else SOUND_ONE_SHOT)
                | SCHANNEL_ENABLE
            )
            chn.tempSOURCE = swav
            chn.tempTIMER = swav.time
            chn.tempREPEAT = swav.loopStart
            chn.tempLENGTH = swav.length
            chn.reg.samplePosition = -3.0

        chn.state = CS_START
        chn.loop_pass_sample = None
        chn.trackId = self.trackId
        chn.flags = [False, False, False]
        chn.prio = self.prio
        chn.key = key
        chn.orgKey = noteDef.noteNumber
        chn.velocity = cnv_sust(vel)
        chn.pan = noteDef.pan - 64
        chn.modDelayCnt = 0
        chn.modCounter = 0
        chn.noteLength = length
        chn.reg.sampleIncrease = 0.0

        chn.attackLvl = cnv_attack(noteDef.attackRate if self.a == 0xFF else self.a)
        chn.decayRate = cnv_fall(noteDef.decayRate if self.d == 0xFF else self.d)
        chn.sustainLvl = noteDef.sustainLevel if self.s == 0xFF else self.s
        chn.releaseRate = cnv_fall(noteDef.releaseRate if self.r == 0xFF else self.r)

        chn.update_vol(self)
        chn.update_pan(self)
        chn.update_tune(self)
        chn.update_mod(self)
        chn.update_porta(self, length if gate is None else gate)
        self.portaKey = key
        return nCh

    def note_on_tie(self, key, vel, gate):
        chn = None
        found = -1
        for i in range(16):
            c = self.ply.channels[i]
            if c.state > CS_NONE and c.trackId == self.trackId and c.state != CS_RELEASE:
                chn = c
                found = i
                break
        if found == -1:
            return self.note_on(key, vel, -1, gate=gate)
        chn.flags = [False, False, False]
        chn.prio = self.prio
        chn.key = key
        chn.velocity = cnv_sust(vel)
        chn.modDelayCnt = 0
        chn.modCounter = 0
        chn.update_vol(self)
        chn.update_tune(self)
        chn.update_mod(self)
        chn.update_porta(self, gate)
        self.portaKey = key
        chn.flags[F_UPDTMR] = True
        return found

    def release_all_notes(self):
        for c in self.ply.channels:
            if c.state > CS_NONE and c.trackId == self.trackId and c.state != CS_RELEASE:
                c.release()

    # --- FSS Track_Run ------------------------------------------------
    def run(self):
        self.updateFlags[TF_LEN] = True
        if self.state[TS_END]:
            return
        if self.waitChn:
            # Nintendo's driver: a note played with length 0 in note-wait mode
            # suspends the track until the note's channel dies (e.g. a one-shot
            # sample finishing). This is how voice clips are chained without
            # knowing their duration in ticks. (Not implemented in FSS/NCSF.)
            for c in self.ply.channels:
                if c.state != CS_NONE and c.trackId == self.trackId:
                    return
            self.waitChn = False
        if self.wait:
            self.wait -= 1
            if self.wait:
                return
        blob = self.ply.blob
        guard = 0
        while not self.wait:
            guard += 1
            if guard > 100000 or self.pos < 0 or self.pos >= len(blob):
                self.state[TS_END] = True
                return
            if self.over_active:
                cmd = self.over_cmd
            else:
                if self.pos not in self.visited:
                    self.visited[self.pos] = self.ply.now_sample
                cmd = self.read8()
            if cmd < 0x80:
                key = (cmd + self.transpose) & 0xFF
                vel = self.oval_read8(extra=True)
                length = self.oval_readvl()
                if self.state[TS_NOTEWAIT]:
                    self.wait = length
                if self.state[TS_TIE]:
                    ch = self.note_on_tie(key, vel, length)
                else:
                    ch = self.note_on(key, vel, length)
                if self.state[TS_NOTEWAIT] and length == 0 and ch >= 0:
                    self.waitChn = True
                    if cmd not in (0xA0, 0xA1):
                        self.over_active = False
                    return
            elif cmd == 0x93: # OpenTrack
                tNum = self.read8()
                trackPos = self.read24()
                newTrack = self.ply.track_alloc()
                if newTrack != -1:
                    self.ply.tracks[newTrack].init(newTrack, self.ply, trackPos, tNum)
                    self.ply.trackIds.append(newTrack)
            elif cmd == 0x80: # Rest
                self.wait = self.oval_readvl()
            elif cmd == 0x81: # Patch
                self.patch = self.oval_readvl()
            elif cmd == 0x94: # Goto
                dest = self.read24()
                if dest in self.visited:
                    # jumping into already-executed code = sequence loop
                    self.ply.loop_detected = True
                    self.ply.mark_loop(self.visited[dest], self.ply.now_sample, self)
                    if self.passes_left > 0:
                        # follow the jump like the driver: same tick, no gap
                        self.passes_left -= 1
                        self.visited = {}
                        self.pos = dest
                    else:
                        self.state[TS_END] = True
                        return
                else:
                    self.pos = dest
            elif cmd == 0x95: # Call
                dest = self.read24()
                if self.stackPos < 3:
                    self.stack[self.stackPos] = ('call', self.pos)
                    self.stackPos += 1
                    self.pos = dest
            elif cmd == 0xFD: # Return
                if self.stackPos and self.stack[self.stackPos - 1][0] == 'call':
                    self.stackPos -= 1
                    self.pos = self.stack[self.stackPos][1]
            elif cmd == 0xC0: # Pan
                self.pan = self.oval_read8() - 64
                self.updateFlags[TF_PAN] = True
            elif cmd == 0xC1: # Volume
                self.vol = self.oval_read8()
                self.updateFlags[TF_VOL] = True
            elif cmd == 0xC2: # MasterVolume
                self.ply.masterVol = cnv_sust(self.oval_read8())
                for tid in self.ply.trackIds:
                    self.ply.tracks[tid].updateFlags[TF_VOL] = True
            elif cmd == 0xC6: # Priority
                self.prio = (self.ply.prio + self.read8()) & 0xFF
            elif cmd == 0xC7: # NoteWait
                self.state[TS_NOTEWAIT] = bool(self.read8())
            elif cmd == 0xC8: # Tie
                self.state[TS_TIE] = bool(self.read8())
                self.release_all_notes()
            elif cmd == 0xD5: # Expression
                self.expr = self.oval_read8()
                self.updateFlags[TF_VOL] = True
            elif cmd == 0xE1: # Tempo
                self.ply.tempo = self.read16()
            elif cmd == 0xFF: # End
                self.state[TS_END] = True
                return
            elif cmd == 0xD4: # LoopStart
                value = self.oval_read8()
                if self.stackPos < 3:
                    self.loopCount[self.stackPos] = value
                    self.stack[self.stackPos] = ('loop', self.pos, self.ply.now_sample)
                    self.stackPos += 1
            elif cmd == 0xFC: # LoopEnd
                if self.stackPos and self.stack[self.stackPos - 1][0] == 'loop':
                    rPos = self.stack[self.stackPos - 1][1]
                    prevR = self.loopCount[self.stackPos - 1]
                    if not prevR:
                        # infinite loop: play loop_passes iterations, then fall through
                        self.ply.loop_detected = True
                        self.ply.mark_loop(self.stack[self.stackPos - 1][2], self.ply.now_sample, self)
                        if self.passes_left > 0:
                            self.passes_left -= 1
                            # restamp the entry sample so the repeat pass
                            # reports (this wrap, next wrap) on re-detection
                            self.stack[self.stackPos - 1] = ('loop', rPos, self.ply.now_sample)
                            self.pos = rPos
                        else:
                            self.stackPos -= 1
                    else:
                        if prevR:
                            self.loopCount[self.stackPos - 1] = prevR - 1
                        if not prevR or prevR - 1:
                            self.pos = rPos
                        else:
                            self.stackPos -= 1
            elif cmd == 0xC3: # Transpose
                self.transpose = s8(self.oval_read8())
            elif cmd == 0xC4: # PitchBend
                self.pitchBend = s8(self.oval_read8())
                self.updateFlags[TF_TIMER] = True
            elif cmd == 0xC5: # PitchBendRange
                self.pitchBendRange = self.read8()
                self.updateFlags[TF_TIMER] = True
            elif cmd == 0xD0: # Attack
                self.a = self.oval_read8()
            elif cmd == 0xD1: # Decay
                self.d = self.oval_read8()
            elif cmd == 0xD2: # Sustain
                self.s = self.oval_read8()
            elif cmd == 0xD3: # Release
                self.r = self.oval_read8()
            elif cmd == 0xC9: # PortamentoKey
                self.portaKey = (self.read8() + self.transpose) & 0xFF
                self.state[TS_PORTA] = True
            elif cmd == 0xCE: # PortamentoFlag
                self.state[TS_PORTA] = bool(self.read8())
            elif cmd == 0xCF: # PortamentoTime
                self.portaTime = self.oval_read8()
            elif cmd == 0xE3: # SweepPitch
                self.sweepPitch = s16(self.oval_read16())
                self.state[TS_PORTA] = True
            elif cmd == 0xCA: # ModulationDepth
                self.modDepth = self.oval_read8()
                self.updateFlags[TF_MOD] = True
            elif cmd == 0xCB: # ModulationSpeed
                self.modSpeed = self.oval_read8()
                self.updateFlags[TF_MOD] = True
            elif cmd == 0xCC: # ModulationType
                self.modType = self.read8()
                self.updateFlags[TF_MOD] = True
            elif cmd == 0xCD: # ModulationRange
                self.modRange = self.read8()
                self.updateFlags[TF_MOD] = True
            elif cmd == 0xE0: # ModulationDelay
                self.modDelay = self.oval_read16()
                self.updateFlags[TF_MOD] = True
            elif cmd == 0xA0: # Random
                self.over_active = True
                self.over_cmd = self.read8()
                if (0xB0 <= self.over_cmd <= 0xBD) or self.over_cmd < 0x80:
                    self.over_extra = self.read8()
                minVal = s16(self.read16())
                maxVal = s16(self.read16())
                self.over_value = (self.ply.rng.calc() % (maxVal - minVal + 1)) + minVal
                continue # keep override active
            elif cmd == 0xA1: # FromVariable
                self.over_active = True
                self.over_cmd = self.read8()
                if (0xB0 <= self.over_cmd <= 0xBD) or self.over_cmd < 0x80:
                    self.over_extra = self.read8()
                self.over_value = self.ply.variables[self.read8() & 0x1F]
                continue # keep override active
            elif 0xB0 <= cmd <= 0xB6 and cmd != 0xB7: # variable ops
                varNo = s8(self.oval_read8(extra=True)) & 0x1F
                value = s16(self.oval_read16())
                var = self.ply.variables[varNo]
                if cmd == 0xB0:
                    var = value
                elif cmd == 0xB1:
                    var = s16(var + value)
                elif cmd == 0xB2:
                    var = s16(var - value)
                elif cmd == 0xB3:
                    var = s16(var * value)
                elif cmd == 0xB4:
                    if value:
                        var = s16(cdiv(var, value))
                elif cmd == 0xB5:
                    var = s16(var >> -value) if value < 0 else s16(var << value)
                elif cmd == 0xB6:
                    r = self.ply.rng.calc()
                    var = -(r % (-value + 1)) if value < 0 else (r % (value + 1))
                self.ply.variables[varNo] = var
            elif 0xB8 <= cmd <= 0xBD:  # comparisons
                varNo = s8(self.oval_read8(extra=True)) & 0x1F
                value = s16(self.oval_read16())
                var = self.ply.variables[varNo]
                if cmd == 0xB8:
                    self.lastComparisonResult = var == value
                elif cmd == 0xB9:
                    self.lastComparisonResult = var >= value
                elif cmd == 0xBA:
                    self.lastComparisonResult = var > value
                elif cmd == 0xBB:
                    self.lastComparisonResult = var <= value
                elif cmd == 0xBC:
                    self.lastComparisonResult = var < value
                else:
                    self.lastComparisonResult = var != value
            elif cmd == 0xA2:
                if not self.lastComparisonResult:
                    nextCmd = self.read8()
                    cmdBytes = sseq_command_byte_count(nextCmd)
                    variableBytes = bool(cmdBytes & VARIABLE_BYTE_COUNT)
                    extraByte = bool(cmdBytes & EXTRA_BYTE)
                    cmdBytes &= ~(VARIABLE_BYTE_COUNT | EXTRA_BYTE)
                    if extraByte:
                        extraCmd = self.read8()
                        if (0xB0 <= extraCmd <= 0xBD) or extraCmd < 0x80:
                            cmdBytes += 1
                    self.pos += cmdBytes
                    if variableBytes:
                        _, self.pos = readvl(blob, self.pos)
            else:
                nb = sseq_command_byte_count(cmd)
                if nb == 0 and cmd not in (0xD6, 0xD7):
                    # unknown command: stop this track defensively
                    self.state[TS_END] = True
                    return
                self.pos += nb & ~(VARIABLE_BYTE_COUNT | EXTRA_BYTE)
                if nb & VARIABLE_BYTE_COUNT:
                    _, self.pos = readvl(blob, self.pos)

            if cmd not in (0xA0, 0xA1):
                self.over_active = False

class Player:
    """
    SSEQ player: 16 hardware channels, 32 tracks, tempo, LCG RNG.

    Args:
        blob: SSEQ event bytecode.
        bank: list of BankEntry (instrument definitions).
        waveArc: list of 4 decoded wave archives (or None).
        sample_rate: output sample rate in Hz.
        sseq_vol: SSEQ master volume (0-127).
        player_prio: channel-allocation priority base from SDAT cpr field.
        loop_passes: number of times the sequence loop body is played.
            1 = current/legacy behavior (one iteration, marks on pass 1)
            2 = steady-state export: tracks follow their backward jump once
    """
    def __init__(self, blob, bank, waveArc, sample_rate, sseq_vol, player_prio=0, loop_passes=1):
        self.blob = blob
        self.bank = bank
        self.waveArc = waveArc
        self.sampleRate = sample_rate
        self.loop_passes = loop_passes
        self.loop_detected = False
        self.loop_start_sample = None
        self.loop_end_sample = None
        self.loop_end2_sample = None
        self._loop_owner = None
        self.ticked = False
        self.now_sample = 0
        self.prio = player_prio
        self.tempo = 120
        self.tempoCount = 0
        self.tempoRate = 0x100
        self.masterVol = 0
        self.sseqVol = sseq_vol
        self.variables = [-1] * 32
        self.rng = Rng()
        self.tracks = [Track() for _ in range(32)]
        self.trackIds = []
        self.channels = [Channel(i, self) for i in range(16)]

    def setup(self, start_offset):
        t = self.track_alloc()
        self.tracks[t].init(t, self, start_offset, 0)
        self.trackIds = [t]

    def track_alloc(self):
        for i, trk in enumerate(self.tracks):
            if not trk.state[TS_ALLOC]:
                trk.zero()
                trk.state[TS_ALLOC] = True
                trk.updateFlags = [False] * 5
                return i
        return -1

    def channel_alloc(self, type_, priority):
        order = (PCM_CHN_ORDER, PSG_CHN_ORDER, NOISE_CHN_ORDER)[type_]
        cur = -1
        for n in order:
            if cur != -1:
                this_c = self.channels[n]
                cur_c = self.channels[cur]
                if this_c.prio >= cur_c.prio:
                    if this_c.prio != cur_c.prio:
                        continue
                    if cur_c.vol <= this_c.vol:
                        continue
            cur = n
        if cur == -1 or priority < self.channels[cur].prio:
            return -1
        self.channels[cur].noteLength = -1
        self.channels[cur].vol = 0x7FF
        return cur

    def mark_loop(self, start_sample, end_sample, track=None):
        if self.loop_start_sample is None:
            self.loop_start_sample = start_sample
            self.loop_end_sample = end_sample
            self._loop_owner = track
        elif (
            track is not None
            and track is self._loop_owner
            and self.loop_end2_sample is None
            and end_sample > self.loop_end_sample
        ):
            self.loop_end2_sample = end_sample

    def run(self):
        while self.tempoCount >= 240:
            self.tempoCount -= 240
            self.ticked = True
            i = 0
            while i < len(self.trackIds):
                self.tracks[self.trackIds[i]].run()
                i += 1
        self.tempoCount += (self.tempo * self.tempoRate) >> 8

    def update_tracks(self):
        for chn in self.channels:
            chn.update_track()
        for trk in self.tracks:
            trk.updateFlags = [False] * 5

    def timer(self):
        self.update_tracks()
        for chn in self.channels:
            chn.update()
        self.run()

    def all_tracks_ended(self):
        return all(self.tracks[t].state[TS_END] for t in self.trackIds)

    def any_channel_active(self):
        return any(c.state != CS_NONE for c in self.channels)
