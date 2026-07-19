"""DS driver math: integer ops, envelope conversions, timer."""

from .tables import GETPITCHTBL, ATTACK_LUT, SCALE_LUT, SUST_LUT, SINE_LUT

ARM7_CLOCK = 33513982
SECONDS_PER_CLOCK = 64.0 * 2728.0 / ARM7_CLOCK # ~1/192.03 s, the mixer's tick
AMPL_K = 723
AMPL_THRESHOLD = -AMPL_K * 128

CS_NONE, CS_START, CS_ATTACK, CS_DECAY, CS_SUSTAIN, CS_RELEASE = range(6)

PCM_CHN_ORDER = (4, 5, 6, 7, 2, 0, 3, 1, 8, 9, 10, 11, 14, 12, 15, 13)
PSG_CHN_ORDER = (8, 9, 10, 11, 12, 13)
NOISE_CHN_ORDER = (14, 15)

SOUND_REPEAT = 1 << 27
SOUND_ONE_SHOT = 1 << 28
SCHANNEL_ENABLE = 1 << 31
SOUND_FORMAT_PSG = 3 << 29

F_UPDVOL, F_UPDPAN, F_UPDTMR = 0, 1, 2
TF_VOL, TF_PAN, TF_TIMER, TF_MOD, TF_LEN = 0, 1, 2, 3, 4
TS_ALLOC, TS_NOTEWAIT, TS_PORTA, TS_TIE, TS_END = 0, 1, 2, 3, 4

def cdiv(a, b):
    """Division truncating toward zero, where Python would floor."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q

def s8(x):
    return ((x + 0x80) & 0xFF) - 0x80

def s16(x):
    return ((x + 0x8000) & 0xFFFF) - 0x8000

def muldiv7(val, mul):
    """Volume scale in 7-bit fixed point, 127 = unity."""
    return val if mul == 127 else (val * mul) >> 7

def cnv_attack(attk):
    """Attack byte to envelope step."""
    if attk & 0x80:
        attk = 0
    return ATTACK_LUT[0x7F - attk] if attk >= 0x6D else 0xFF - attk

def cnv_fall(fall):
    """Decay byte to envelope step."""
    if fall & 0x80:
        fall = 0
    if fall == 0x7F:
        return 0xFFFF
    if fall == 0x7E:
        return 0x3C00
    if fall < 0x32:
        return ((fall << 1) + 1) & 0xFFFF
    return (0x1E00 // (0x7E - fall)) & 0xFFFF

def cnv_scale(scale):
    """Scale byte to fixed-point level."""
    if scale & 0x80:
        scale = 0x7F
    return SCALE_LUT[scale]

def cnv_sust(sust):
    """Sustain byte to fixed-point level."""
    if sust & 0x80:
        sust = 0x7F
    return SUST_LUT[sust]

def cnv_sine(arg):
    """Sine at a 128-step phase."""
    arg &= 0x7F
    if arg <= 32:
        return SINE_LUT[arg]
    if arg <= 64:
        return SINE_LUT[64 - arg]
    if arg <= 96:
        return -SINE_LUT[arg - 64]
    return -SINE_LUT[128 - arg]

def timer_adjust(basetmr, pitch):
    """Timer reload for a base timer bent by pitch, clamped to 0x10..0xFFFF."""
    shift = 0
    pitch = -pitch
    while pitch < 0:
        shift -= 1
        pitch += 0x300
    while pitch >= 0x300:
        shift += 1
        pitch -= 0x300
    tmr = basetmr * (GETPITCHTBL[pitch] + 0x10000)
    shift -= 16
    if shift <= 0:
        tmr >>= -shift
    elif shift < 32:
        if tmr >> (32 - shift):
            return 0xFFFF
        tmr <<= shift
    else:
        return 0xFFFF
    if tmr < 0x10:
        return 0x10
    if tmr > 0xFFFF:
        return 0xFFFF
    return tmr

def calc_voldiv_shift(x):
    """Volume-divider shift, capped at 4."""
    return x if x < 3 else 4
