"""3DS voice synthesis and DSP aux-bus effects."""

from .cprims import CS_NONE, CS_RELEASE

class DspDelay:
    """Aux-bus delay line."""

    SAMPLES_PER_FRAME = 160

    def __init__(self, frame_count, g, a, b):
        self.N = max(1, int(frame_count) * self.SAMPLES_PER_FRAME)
        self.g = g
        self.a = a
        self.b = b
        self.xbuf = [0.0] * self.N
        self.ybuf = [0.0] * self.N
        self.idx = 0
        self.y1 = 0.0

    def process(self, xs):
        N = self.N
        g = self.g
        a = self.a
        b = self.b
        xbuf = self.xbuf
        ybuf = self.ybuf
        idx = self.idx
        y1 = self.y1
        out = [0.0] * len(xs)
        for i, x in enumerate(xs):
            y = a * xbuf[idx] + b * y1 - a * g * ybuf[idx]
            xbuf[idx] = x
            ybuf[idx] = y
            idx += 1
            if idx == N:
                idx = 0
            y1 = y
            out[i] = y
        self.idx = idx
        self.y1 = y1
        return out

class AuxEffect:
    """One aux bus: a stereo effect plus return volume."""

    def __init__(self, kind, params, ret):
        self.kind = kind
        self.ret = ret
        if kind == 'delay':
            self.left = DspDelay(**params)
            self.right = DspDelay(**params)
        else:
            raise ValueError('unknown effect %r' % kind)

    def process(self, l, r):
        return self.left.process(l), self.right.process(r)

def parse_fx_spec(spec):
    """Effect spec string to AuxEffect."""
    kind, _, rest = spec.partition(':')
    kv = dict(p.split('=') for p in rest.split(',') if p)
    ret = float(kv.pop('return', 1.0))
    if kind == 'delay':
        params = {'frame_count': int(kv.pop('frames')), 'g': float(kv.pop('g')), 'a': float(kv.pop('a')), 'b': float(kv.pop('b'))}
    else:
        raise ValueError('unknown effect kind %r' % kind)
    if kv:
        raise ValueError('unknown effect parameters: %s' % ', '.join(kv))
    return AuxEffect(kind, params, ret)

class Voice:
    """One CSEQ software voice."""
    __slots__ = ('state', 'trackId', 'prio', 'key', 'org_key', 'velocity',
        'pan', 'region_pan', 'ampl', 'ext_ampl', 'ext_pan', 'ext_tune',
        'attackLvl', 'decayRate', 'sustainLvl', 'releaseRate',
        'noteLength', 'samples', 'base_rate', 'pitch_mul', 'loop',
        'loop_start', 'pos', 'inc', 'vol_l', 'vol_r', 'vol',
        'modType', 'modSpeed', 'modDepth', 'modRange', 'modDelay',
        'modDelayCnt', 'modCounter', 'sweepPitch', 'sweepLen',
        'sweepCnt', 'manualSweep', 'flags', 'region_vol',
        'send_main', 'send_a', 'send_b', 'dead_wrap')

    def __init__(self):
        self.state = CS_NONE
        self.trackId = -1
        self.prio = 0
        self.vol = 0
        self.noteLength = -1

    def release(self):
        self.noteLength = -1
        self.prio = 1
        self.state = CS_RELEASE

    def kill(self):
        self.state = CS_NONE
        self.trackId = -1
        self.prio = 0
        self.vol = 0
        self.noteLength = -1
