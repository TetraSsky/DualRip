"""3DS CSEQ driver math: integer ops, envelope conversions, LUTs."""

AMPL_K = 723
AMPL_THRESHOLD = -AMPL_K * 128
DRIVER_HZ = 192.03 # driver mix rate

CS_NONE, CS_START, CS_ATTACK, CS_DECAY, CS_SUSTAIN, CS_RELEASE = range(6)
TS_NOTEWAIT, TS_PORTA, TS_TIE, TS_END = range(4)

ATTACK_LUT = (0, 1, 5, 14, 26, 38, 51, 63, 73, 84, 92, 100, 109, 116, 123, 127, 132, 137, 143)

SUST_LUT = (
    -32768, -722, -721, -651, -601, -562, -530, -503,
    -480, -460, -442, -425, -410, -396, -383, -371,
    -360, -349, -339, -330, -321, -313, -305, -297,
    -289, -282, -276, -269, -263, -257, -251, -245,
    -239, -234, -229, -224, -219, -214, -210, -205,
    -201, -196, -192, -188, -184, -180, -176, -173,
    -169, -165, -162, -158, -155, -152, -149, -145,
    -142, -139, -136, -133, -130, -127, -125, -122,
    -119, -116, -114, -111, -109, -106, -103, -101,
    -99, -96, -94, -91, -89, -87, -85, -82,
    -80, -78, -76, -74, -72, -70, -68, -66,
    -64, -62, -60, -58, -56, -54, -52, -50,
    -49, -47, -45, -43, -42, -40, -38, -36,
    -35, -33, -31, -30, -28, -27, -25, -23,
    -22, -20, -19, -17, -16, -14, -13, -11,
    -10, -8, -7, -6, -4, -3, -1, 0,
)

SINE_LUT = (
    0, 6, 12, 19, 25, 31, 37, 43, 49, 54, 60,
    65, 71, 76, 81, 85, 90, 94, 98, 102, 106, 109,
    112, 115, 117, 120, 122, 123, 125, 126, 126, 127, 127,
)

def cnv_sine(arg):
    arg &= 0x7F
    if arg <= 32:
        return SINE_LUT[arg]
    if arg <= 64:
        return SINE_LUT[64 - arg]
    if arg <= 96:
        return -SINE_LUT[arg - 64]
    return -SINE_LUT[128 - arg]

def cdiv(a, b):
    """Division truncating toward zero, where Python would floor."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q

def s8(x):
    return ((x + 0x80) & 0xFF) - 0x80

def s16(x):
    return ((x + 0x8000) & 0xFFFF) - 0x8000

def cnv_attack(attk):
    if attk & 0x80:
        attk = 0
    return ATTACK_LUT[0x7F - attk] if attk >= 0x6D else 0xFF - attk

def cnv_fall(fall):
    if fall & 0x80:
        fall = 0
    if fall == 0x7F:
        return 0xFFFF
    if fall == 0x7E:
        return 0x3C00
    if fall < 0x32:
        return ((fall << 1) + 1) & 0xFFFF
    return (0x1E00 // (0x7E - fall)) & 0xFFFF

def cnv_sust(sust):
    if sust & 0x80:
        sust = 0x7F
    return SUST_LUT[sust]

def readvl(blob, pc):
    value = 0
    while True:
        b = blob[pc]
        pc += 1
        value = (value << 7) | (b & 0x7F)
        if not (b & 0x80):
            return value, pc

class Rng:
    """Driver RNG (LCG)."""
    def __init__(self):
        self.state = 0x12345678

    def calc(self):
        self.state = (self.state * 0x19660D + 0x3C6EF35F) & 0xFFFFFFFF
        return self.state >> 16

