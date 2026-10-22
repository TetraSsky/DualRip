"""
Golden fidelity tests — any engine change that alters audio shows up here.

NDS: requires DUALRIP_TEST_SDAT env var pointing to a real sound_data.sdat, or to a ROM that carries one.
3DS: requires DUALRIP_TEST_CTR env var pointing to a real 3DS ROM (.cia/.3ds) or a loose .bcsar sound archive.
NDS streams: requires DUALRIP_TEST_STRM env var pointing to an SDAT or a ROM whose audio is streamed (STRM, IMA-ADPCM), and optionally DUALRIP_TEST_STRM_PCM8 for an archive streamed as PCM8.
DSE: requires DUALRIP_TEST_DSE env var pointing to a ROM whose audio uses the DSE driver.
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

    if SDAT.lower().endswith('.sdat'):
        return SdatFile(SDAT)
    from dualrip.formats.sdat.rom import find_sdats_in_rom

    found = find_sdats_in_rom(SDAT)
    assert found, 'no SDAT inside DUALRIP_TEST_SDAT'
    return SdatFile.from_bytes(found[0]['data'], label=os.path.basename(SDAT))

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

# (archive, entry) -> reason reported for a silent entry
EMPTY_GOLDEN = {
    (0, 0): 'unused slot',
    (0, 11): 'unused slot',
    (1, 247): 'unused slot',
    (2, 130): 'no notes',
    (1, 34): 'wave archive slot empty',
    (2, 163): 'patch empty in bank',
}

@pytest.mark.parametrize('arc_id,index', sorted(EMPTY_GOLDEN))
def test_empty_entry_reason(sdat, arc_id, index):
    from dualrip import BankResolver, render_one

    seqarc = sdat.seqarc(arc_id)
    res = render_one(sdat, seqarc, seqarc.entries[index], 44100, BankResolver(sdat, seqarc))
    assert res.status == 'empty'
    assert res.reason == EMPTY_GOLDEN[(arc_id, index)]

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

# sound name -> reason reported when it renders silent
EMPTY_GOLDEN_CTR = {
    'SE_KT_SLIP_NONE': 'unused slot',
    'SE_KT_HORN_KOOPA': 'notes rejected by bank',
    'SE_EN_KILLER_CANNON_MOVE': 'entry volume zero',
}

@pytest.mark.parametrize('name', sorted(EMPTY_GOLDEN_CTR))
def test_empty_reason_ctr(archive, name):
    from dualrip.export import render_ctr_one

    sound = next((s for s in archive.sounds if s.name == name), None)
    if sound is None:
        pytest.skip(f'{name} not in this archive')
    res, _chans, _rate, _loop = render_ctr_one(archive, sound, 16000, keep_audio=False)
    assert res.status == 'empty'
    assert res.reason == EMPTY_GOLDEN_CTR[name]

# ---------------------------------------------------------------------------
# NDS streams (SDAT: STRM)
# ---------------------------------------------------------------------------

STRM_FILE = os.environ.get('DUALRIP_TEST_STRM')
STRM_PCM8_FILE = os.environ.get('DUALRIP_TEST_STRM_PCM8')

# stream index -> sha256 of the int16 stereo PCM the preview plays
GOLDEN_STRM = {
    # covers: long stereo IMA-ADPCM stream, looped stream, short looped stream
    0: '780ade597f3a1341e0107267071859d2a8a0803a27f601457a5f4df1a9983c7d',
    1: 'f74e3bacd469360b10b7cdae85b6fbb52185f2ff495088aa4d84ac31afc2af94',
    2: 'bc92385c0c6bde543d9b8a8934fbf0481eec43fa16eaeb42b95966e9ae9d66c4',
}

# stream index -> sha256, for an archive whose streams are PCM8
GOLDEN_STRM_PCM8 = {
    0: '2c89d187453647096ec80216a336db3c8aecc1eb46be1cc3fd96edc91290832c',
    9: '2570cd7cd06f3dc7233232a35237c25ff221286625a5795088c7778a20ca2781',
}


def _strm_archive(path):
    """SdatFile of a .sdat file, or of the first SDAT carrying streams"""
    from dualrip import SdatFile

    if path.lower().endswith('.sdat'):
        return SdatFile(path)
    from dualrip.formats.sdat.rom import find_sdats_in_rom

    for found in find_sdats_in_rom(path):
        sdat = SdatFile.from_bytes(found['data'], label=os.path.basename(path))
        if sdat.stream_list:
            return sdat
    return None

@pytest.fixture(scope='module')
def strm_sdat():
    if not STRM_FILE or not os.path.exists(STRM_FILE):
        pytest.skip('DUALRIP_TEST_STRM not set')
    sdat = _strm_archive(STRM_FILE)
    assert sdat is not None, 'no SDAT with streams in DUALRIP_TEST_STRM'
    return sdat

@pytest.fixture(scope='module')
def strm_pcm8_sdat():
    if not STRM_PCM8_FILE or not os.path.exists(STRM_PCM8_FILE):
        pytest.skip('DUALRIP_TEST_STRM_PCM8 not set')
    sdat = _strm_archive(STRM_PCM8_FILE)
    assert sdat is not None, 'no SDAT with streams in DUALRIP_TEST_STRM_PCM8'
    return sdat

@pytest.mark.parametrize('index', sorted(GOLDEN_STRM))
def test_golden_stream(strm_sdat, index):
    from dualrip.export import render_strm_one

    name = dict((i, n) for i, n, _v in strm_sdat.stream_list)[index]
    res, _audio, _rate, _loop = render_strm_one(strm_sdat, index, name)
    assert res.status in ('ok', 'loop'), res.error
    digest = hashlib.sha256(res.audio.tobytes()).hexdigest()
    expected = GOLDEN_STRM[index]
    if expected is None:
        # first run: print the hash to pin in GOLDEN_STRM
        print(f'\nGOLDEN_STRM[{index}] = {digest!r}')
    else:
        assert digest == expected

@pytest.mark.parametrize('index', sorted(GOLDEN_STRM_PCM8))
def test_golden_stream_pcm8(strm_pcm8_sdat, index):
    from dualrip.export import render_strm_one

    name = dict((i, n) for i, n, _v in strm_pcm8_sdat.stream_list)[index]
    res, _audio, _rate, _loop = render_strm_one(strm_pcm8_sdat, index, name)
    assert res.status in ('ok', 'loop'), res.error
    digest = hashlib.sha256(res.audio.tobytes()).hexdigest()
    expected = GOLDEN_STRM_PCM8[index]
    if expected is None:
        # first run: print the hash to pin in GOLDEN_STRM_PCM8
        print(f'\nGOLDEN_STRM_PCM8[{index}] = {digest!r}')
    else:
        assert digest == expected

@pytest.mark.parametrize('fixture_name', ('strm_sdat', 'strm_pcm8_sdat'))
def test_stream_header_matches_decode(request, fixture_name):
    """Declared sample count, channels and block arithmetic agree with the decode."""
    sdat = request.getfixturevalue(fixture_name)
    for sid, name, _vol in sdat.stream_list:
        st = sdat.stream(sid)
        audio = st.decode()
        assert audio.shape == (st.num_samples, st.channels), name
        assert st.num_samples == (
            (st.num_blocks - 1) * st.block_samples + st.last_block_samples), name
        if st.wave_type == 2:
            assert (st.block_size - 4) * 2 == st.block_samples, name
        elif st.wave_type == 0:
            assert st.block_size == st.block_samples, name
        else:
            assert st.block_size == st.block_samples * 2, name

# ---------------------------------------------------------------------------
# DSE (SMDL + SEDL + SADL)
# ---------------------------------------------------------------------------

DSE_FILE = os.environ.get('DUALRIP_TEST_DSE')

# sound name -> reason reported when it renders silent
EMPTY_GOLDEN_DSE = {
    'GE_002_04': 'track expression zero',
    'SI_100_06': 'sample missing',
}

@pytest.fixture(scope='module')
def dse_archive():
    if not DSE_FILE or not os.path.exists(DSE_FILE):
        pytest.skip('DUALRIP_TEST_DSE not set')
    from dualrip.formats.dse import open_dse_rom

    return open_dse_rom(DSE_FILE)

@pytest.mark.parametrize('name', sorted(EMPTY_GOLDEN_DSE))
def test_empty_reason_dse(dse_archive, name):
    """A silent DSE sound names its cause: a skipped note or a muted track."""
    from dualrip.export import render_dse_one

    entry = next((s for s in dse_archive.sounds if s.name == name), None)
    if entry is None:
        pytest.skip(f'{name} not in this archive')
    res, _audio, _rate, _loop = render_dse_one(dse_archive, entry, 32728, keep_audio=False)
    assert res.status == 'empty'
    assert res.reason == EMPTY_GOLDEN_DSE[name]
