"""Golden fidelity tests — any engine change that alters audio shows up here.

Requires DUALRIP_TEST_SDAT env var pointing to a real sound_data.sdat.
"""

import hashlib
import os

import pytest

SDAT = os.environ.get('DUALRIP_TEST_SDAT')

# (archive, entry) -> sha256 of the int16 stereo PCM (not the WAV container)
GOLDEN = {
    # covers: portamento glide, tie+sweep, notewait, voice chaining (len=0),
    # variables, layered notes, looped-sample cut + loop marks,
    # sequence loop, auto bank resolution
    (2, 0): '7c876ff716eaaa3e00156d3927aa277344c7ba8483cd7e8a64c6094e967bc781',
    (2, 9): '79635cc84e33fb71cef51984e13e10f55e8aba33ea287410e9e92edf5cd96e31',
    (2, 14): 'fae2ad44d58164462ae08c3455ad8edb50204ef55cf57a0aeed30ce2543dba87',
    (2, 17): 'c0074808e958fea54d019e9a85c17bf84ac8e027b0a61427a75c2c9741b208c6',
    (2, 47): '0997f9b374621c97be0e4f34e02f60ced88bd0450faed1c3e9da5d489427537d',
    (2, 111): 'c150f439787b8addd082dc5be359e48dd95f70cb788935fc2eee9f1dcc3edff7',
    (2, 284): '2ad13c8ed50b91ff73652288c6d2814868a5da8fb97503b87ff97d94a36c7042',
    (3, 58): 'c9f2e8e0a4a2d823e57aa213b437c03fee8826bdca28b2d2023621f140ef40dc',
}


@pytest.fixture(scope='module')
def sdat():
    if not SDAT or not os.path.exists(SDAT):
        pytest.skip('DUALRIP_TEST_SDAT not set')
    from dualrip import SdatFile

    return SdatFile(SDAT)


@pytest.mark.parametrize('arc_id,index', sorted(GOLDEN))
def test_golden_render(sdat, arc_id, index):
    from dualrip import BankResolver, render_one

    seqarc = sdat.seqarc(arc_id)
    res = render_one(sdat, seqarc, seqarc.entries[index], 44100, BankResolver(sdat, seqarc))
    assert res.status in ('ok', 'loop'), res.error
    digest = hashlib.sha256(res.audio.tobytes()).hexdigest()
    expected = GOLDEN[(arc_id, index)]
    if expected is None:
        # first run: print the hash to pin in GOLDEN
        print(f'\nGOLDEN[({arc_id}, {index})] = {digest!r}')
    else:
        assert digest == expected


def test_render_is_deterministic(sdat):
    from dualrip import BankResolver, render_one

    seqarc = sdat.seqarc(2)
    r = BankResolver(sdat, seqarc)
    a = render_one(sdat, seqarc, seqarc.entries[0], 44100, r)
    b = render_one(sdat, seqarc, seqarc.entries[0], 44100, r)
    assert (a.audio == b.audio).all()
