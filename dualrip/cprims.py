# Part of DualRip. Core playback logic is a faithful Python port of the FeOS
# Sound System (fincs), as adapted by Naram Qashat (CyberBotX) for the NCSF
# player (github.com/CyberBotX/in_xsf, src/in_ncsf/SSEQPlayer). Lookup tables
# come from disassembly of Nintendo's NNS sound driver by those authors.
# FIDELITY-CRITICAL: C integer semantics (truncating division, arithmetic
# shifts, table indexing) are intentional. Do not "simplify".

from .tables import GETPITCHTBL, ATTACK_LUT, SCALE_LUT, SUST_LUT, SINE_LUT

ARM7_CLOCK = 33513982
SECONDS_PER_CLOCK = 64.0 * 2728.0 / ARM7_CLOCK # ~1/192.03 s, hardware driver rate
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
    """C integer division (truncates toward zero)."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q

def s8(x):
    return ((x + 0x80) & 0xFF) - 0x80

def s16(x):
    return ((x + 0x8000) & 0xFFFF) - 0x8000

def muldiv7(val, mul):
    """Fixed-point multiply: (val * mul) / 128, with mul=127 as identity."""
    return val if mul == 127 else (val * mul) >> 7

def cnv_attack(attk):
    """Convert NNS attack byte to internal step (LUT + linear tail)."""
    if attk & 0x80:
        attk = 0
    return ATTACK_LUT[0x7F - attk] if attk >= 0x6D else 0xFF - attk

def cnv_fall(fall):
    """Convert NNS decay byte to internal step (piecewise with sentinel values)."""
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
    """Convert NNS scale byte via LUT."""
    if scale & 0x80:
        scale = 0x7F
    return SCALE_LUT[scale]

def cnv_sust(sust):
    """Convert NNS sustain byte via LUT."""
    if sust & 0x80:
        sust = 0x7F
    return SUST_LUT[sust]

def cnv_sine(arg):
    """Quarter-wave sine LUT lookup (128-step period)."""
    arg &= 0x7F
    if arg <= 32:
        return SINE_LUT[arg]
    if arg <= 64:
        return SINE_LUT[64 - arg]
    if arg <= 96:
        return -SINE_LUT[arg - 64]
    return -SINE_LUT[128 - arg]

def timer_adjust(basetmr, pitch):
    """
    Hardware timer reload value from base timer + pitch bend.

    Ported from NNS driver disassembly. Returns 0xFFFF on overflow, 0x10 floor.
    """
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
    """Volume divider shift for channel mixing (clamped to 4)."""
    return x if x < 3 else 4
