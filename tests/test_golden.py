"""
Golden fidelity tests — any engine change that alters audio shows up here.

NDS: requires DUALRIP_TEST_SDAT env var pointing to a real sound_data.sdat.
3DS: requires DUALRIP_TEST_CTR env var pointing to a real 3DS ROM (.cia/.3ds)
     or a loose .bcsar sound archive.
Each half skips independently when its env var is unset.
"""

import hashlib
import os
import pytest

# ---------------------------------------------------------------------------
# NDS (SDAT: SSAR + SSEQ)
# ---------------------------------------------------------------------------

SDAT = os.environ.get('DUALRIP_TEST_SDAT')

# (archive, entry) -> sha256 of the int16 stereo PCM (not the WAV container)
GOLDEN = {
    # covers: portamento glide, tie+sweep, notewait, voice chaining (len=0),
    # variables, layered notes, looped-sample cut + loop marks,
    # sequence loop, auto bank resolution, two-pass steady-state loop export
    # (carrying body-end tails across the wrap like real hardware)
    (2, 0): '7c876ff716eaaa3e00156d3927aa277344c7ba8483cd7e8a64c6094e967bc781',
    (2, 9): '79635cc84e33fb71cef51984e13e10f55e8aba33ea287410e9e92edf5cd96e31',
    (2, 14): 'fae2ad44d58164462ae08c3455ad8edb50204ef55cf57a0aeed30ce2543dba87',
    (2, 17): 'c0074808e958fea54d019e9a85c17bf84ac8e027b0a61427a75c2c9741b208c6',
    (2, 47): '0997f9b374621c97be0e4f34e02f60ced88bd0450faed1c3e9da5d489427537d',
    (2, 111): 'c150f439787b8addd082dc5be359e48dd95f70cb788935fc2eee9f1dcc3edff7',
    (2, 284): '1ab14ace7f10b7d30fb6af912a464d63e6adca493675dc8a306f61fe64e3f32b',
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


# ---------------------------------------------------------------------------
# 3DS (CSAR: CSEQ + CWSD + BCSTM)
# ---------------------------------------------------------------------------

CTR_FILE = os.environ.get('DUALRIP_TEST_CTR')

# sound index -> sha256 of the int16 stereo PCM (not the WAV container)
GOLDEN_CTR = {
    # covers: sequence sound, multiple banks (runtime bank-select opcode),
    # looped sample cut + loop marks, sequence loop, auto bank resolution,
    # two-pass steady-state loop export
    # (carrying body-end tails across the wrap like real hardware)
    127: 'c283b7f15d854fe00a3da6e879bce0fe23a962cdd24fa8c19e7ac9fd7274917c',
    93: '4d9d523a015d6b168f69aa9314e1aa9292cfd2ed931793c835e3bc053d4441a8',
    138: 'a5faa17b88ed50f8e27ae17f1733cd56ad22beb16aaa211ee13c05eac1c1c546',
    88: '9ebedd07c3cb9be624d8b93f94215d56dc0f87618025e36c95a8550268dbfa92',
    0: '8a5754c2acfc7c22596e3bce05f8c7881661ce6406af329a434216ed57b6177e',
    1904: '226e1e16624064c1b01d5d1435f0d973989f502f0fdab0f47e62b14414b3f5e7',
}

@pytest.fixture(scope='module')
def archive():
    if not CTR_FILE or not os.path.exists(CTR_FILE):
        pytest.skip('DUALRIP_TEST_CTR not set')
    from dualrip.formats.ctr import find_csars_in_rom, open_bcsar

    if CTR_FILE.lower().endswith('.bcsar'):
        return open_bcsar(CTR_FILE)
    return find_csars_in_rom(CTR_FILE, boot9=os.environ.get('DUALRIP_TEST_BOOT9'))[0]

@pytest.mark.parametrize('index', sorted(GOLDEN_CTR))
def test_golden_render_ctr(archive, index):
    from dualrip.export import render_ctr_one

    sound = archive.sound(index)
    res, _chans, _rate, _loop = render_ctr_one(archive, sound, 44100)
    assert res.status != 'error', res.error
    digest = hashlib.sha256(res.audio.tobytes()).hexdigest()
    expected = GOLDEN_CTR[index]
    if expected is None:
        # first run: print the hash to pin in GOLDEN_CTR
        print(f'\nGOLDEN_CTR[{index}] = {digest!r}')
    else:
        assert digest == expected

def test_render_is_deterministic_ctr(archive):
    from dualrip.export import render_ctr_one

    sound = archive.sound(next(iter(GOLDEN_CTR)))
    a, *_ = render_ctr_one(archive, sound, 44100)
    b, *_ = render_ctr_one(archive, sound, 44100)
    assert (a.audio == b.audio).all()
