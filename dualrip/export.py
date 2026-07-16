"""WAV export and batch rip."""

import csv
import os
import struct
import numpy as np
from .bankmap import BankResolver
from .engine.sdat.render import render_entry

def write_wav(path, stereo, rate, loop=None):
    """Minimal RIFF writer, loop points go in a smpl chunk."""
    data = stereo.tobytes()
    # fmt: PCM (1), stereo, rate, byte rate, block align (2ch x 16bit), bits
    fmt = struct.pack('<HHIIHH', 1, 2, rate, rate * 4, 4, 16)
    chunks = [(b'fmt ', fmt), (b'data', data)]
    if loop is not None:
        start, end = int(loop[0]), int(loop[1])
        end = max(end - 1, start) # smpl dwEnd is the last sample included
        # smpl header: manufacturer, product, sample period (ns), MIDI unity note, pitch fraction, SMPTE, 1 loop, 0 extra
        smpl = struct.pack('<9I', 0, 0, 1000000000 // rate, 60, 0, 0, 0, 1, 0)
        # loop record: id, type (0 = forward), start, end, fraction, count (inf)
        smpl += struct.pack('<6I', 0, 0, start, end, 0, 0)
        chunks.append((b'smpl', smpl))
    payload = b''
    for cid, cdata in chunks:
        payload += cid + struct.pack('<I', len(cdata)) + cdata
        if len(cdata) & 1:
            payload += b'\x00'
    with open(path, 'wb') as f:
        f.write(b'RIFF' + struct.pack('<I', 4 + len(payload)) + b'WAVE' + payload)

def sanitize(name):
    return ''.join(c if c.isalnum() or c in '_-' else '_' for c in name)

class RenderResult:
    """Outcome of rendering one entry (audio kept in memory)."""

    __slots__ = (
        'index',
        'name',
        'status',
        'bank_label',
        'duration',
        'loop_start',
        'loop_end',
        'audio',
        'error',
        'rate',
    )

    def __init__(
        self,
        index,
        name,
        status,
        bank_label='',
        duration=0.0,
        loop_start=None,
        loop_end=None,
        audio=None,
        error='',
        rate=None,
    ):
        self.index = index
        self.name = name
        self.status = status # ok | loop | empty | null | error
        self.bank_label = bank_label
        self.duration = duration
        self.loop_start = loop_start # seconds, or None
        self.loop_end = loop_end
        self.audio = audio # int16 stereo ndarray, or None
        self.error = error
        self.rate = rate # BCSTM rate (3DS Streams)

def render_one(sdat, seqarc, entry, rate=44100, resolver=None):
    """Render one entry in memory."""
    if entry.offset is None:
        return RenderResult(entry.index, entry.name, 'null')
    if resolver is None:
        resolver = BankResolver(sdat, seqarc)
    try:
        rbid = resolver.resolve(entry)
        label = str(entry.bank_id) if rbid == entry.bank_id else f'{entry.bank_id}->{rbid}'
        bank, wave_arc = sdat.bank(rbid)
        audio, looped, marks = render_entry(seqarc.blob, entry.offset, bank, wave_arc, entry.volume, rate, player_prio=entry.cpr or 0, loop_passes=2)
        peak = int(np.abs(audio.astype(np.int32)).max()) if len(audio) else 0
        if len(audio) == 0 or peak == 0:
            return RenderResult(entry.index, entry.name, 'empty', label)
        ls = marks[0] / rate if marks else None
        le = marks[1] / rate if marks else None
        return RenderResult(
            entry.index,
            entry.name,
            'loop' if looped else 'ok',
            label,
            len(audio) / rate,
            ls,
            le,
            audio,
        )
    except Exception as exc: # keep ripping the rest of the archive
        return RenderResult(entry.index, entry.name, 'error', str(entry.bank_id), error=str(exc))

def _manifest_row(index, res, entry_volume):
    """One manifest.csv row shared by the SSAR and SSEQ writers."""
    return [
        index,
        res.name,
        res.bank_label,
        '' if entry_volume is None else entry_volume,
        (
            round(res.duration, 3)
            if res.status in ('ok', 'loop')
            else (0.0 if res.status == 'empty' else '')
        ),
        round(res.loop_start, 3) if res.loop_start is not None else '',
        round(res.loop_end, 3) if res.loop_end is not None else '',
        res.status if res.status != 'error' else f'ERROR: {res.error}',
    ]

MANIFEST_HEADER = [
    'index',
    'name',
    'bank',
    'entry_volume',
    'duration_s',
    'loop_start_s',
    'loop_end_s',
    'status',
]

def rip_sequences(
    sdat,
    seq_ids,
    out_root,
    rate=44100,
    override_map=None,
    progress=None,
    should_cancel=None,
):
    """Rip standalone SSEQ music to WAV and manifest.csv."""
    out_dir = os.path.join(out_root, 'SSEQ')
    os.makedirs(out_dir, exist_ok=True)

    manifest = []
    counts = {'ok': 0, 'loop': 0, 'empty': 0, 'null': 0, 'error': 0}
    note = None
    cancelled = False
    for done, sid in enumerate(seq_ids, 1):
        seqarc = sdat.sequence(sid)
        entry = seqarc.entries[0]
        resolver = BankResolver(sdat, seqarc, override_map)
        if note is None and resolver.note:
            note = resolver.note
        res = render_one(sdat, seqarc, entry, rate, resolver)
        counts[res.status] += 1
        if res.status in ('ok', 'loop'):
            fn = f'{sid:03d}_{sanitize(res.name)}.wav'
            marks = None
            if res.loop_start is not None:
                marks = (round(res.loop_start * rate), round(res.loop_end * rate))
            write_wav(os.path.join(out_dir, fn), res.audio, rate, loop=marks)
        manifest.append(_manifest_row(sid, res, entry.volume))
        res.audio = None # free memory during long batches
        if progress is not None:
            progress(done, len(seq_ids), res)
        if should_cancel is not None and should_cancel():
            cancelled = True
            break

    with open(os.path.join(out_dir, 'manifest.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(MANIFEST_HEADER)
        w.writerows(manifest)

    return {
        'arc_id': 'SSEQ',
        'arc_name': 'SSEQ (music)',
        'out_dir': out_dir,
        'note': note,
        'cancelled': cancelled,
        'total': len(seq_ids),
        **counts,
    }


def rip_archive(
    sdat,
    arc_id,
    out_root,
    rate=44100,
    override_map=None,
    only=None,
    progress=None,
    should_cancel=None,
):
    """Rip one sequence archive to WAV and manifest.csv."""
    seqarc = sdat.seqarc(arc_id)
    resolver = BankResolver(sdat, seqarc, override_map)
    out_dir = os.path.join(out_root, f'{arc_id:03d}_{seqarc.name}')
    os.makedirs(out_dir, exist_ok=True)

    todo = [e for e in seqarc.entries if only is None or e.index in only]
    manifest = []
    counts = {'ok': 0, 'loop': 0, 'empty': 0, 'null': 0, 'error': 0}
    cancelled = False
    for done, entry in enumerate(todo, 1):
        res = render_one(sdat, seqarc, entry, rate, resolver)
        counts[res.status] += 1
        if res.status in ('ok', 'loop'):
            fn = f'{entry.index:03d}_{sanitize(res.name)}.wav'
            marks = None
            if res.loop_start is not None:
                marks = (round(res.loop_start * rate), round(res.loop_end * rate))
            write_wav(os.path.join(out_dir, fn), res.audio, rate, loop=marks)
        manifest.append(_manifest_row(entry.index, res, entry.volume))
        res.audio = None # free memory during long batches
        if progress is not None:
            progress(done, len(todo), res)
        if should_cancel is not None and should_cancel():
            cancelled = True
            break

    with open(os.path.join(out_dir, 'manifest.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(MANIFEST_HEADER)
        w.writerows(manifest)

    return {
        'arc_id': arc_id,
        'arc_name': seqarc.name,
        'out_dir': out_dir,
        'note': resolver.note,
        'cancelled': cancelled,
        'total': len(todo),
        **counts,
    }

def _ctr_chans_to_stereo(chans):
    """Per-channel int16 lists to a stereo int16 ndarray."""
    if not chans or not len(chans[0]):
        return np.zeros((0, 2), dtype=np.int16)
    left = np.asarray(chans[0], dtype=np.int16)
    right = np.asarray(chans[1], dtype=np.int16) if len(chans) > 1 else left
    return np.column_stack((left, right))

def render_ctr_one(archive, sound, rate, keep_audio=True):
    """Render one CSAR sound in memory."""
    from .formats.ctr import KIND_LABEL
    try:
        native_rate, chans, loop = archive.render(sound, rate)
    except (LookupError, ValueError, NotImplementedError) as exc:
        return (RenderResult(sound.index, sound.name, 'error', error=str(exc)), None, None, None)
    n = len(chans[0]) if chans else 0
    peak = max((abs(x) for ch in chans for x in ch), default=0)
    ls = loop[0] / native_rate if loop else None
    le = loop[1] / native_rate if loop else None
    status = 'empty' if (n == 0 or peak == 0) else ('loop' if loop else 'ok')
    res = RenderResult(sound.index, sound.name, status, KIND_LABEL[sound.kind], n / native_rate if n else 0.0, ls, le, _ctr_chans_to_stereo(chans) if keep_audio else None, rate=native_rate,)
    return res, chans, native_rate, loop

CTR_MANIFEST_HEADER = ['index', 'name', 'kind', 'duration_s', 'peak', 'loop_start_s', 'loop_end_s', 'status']

def rip_ctr_folder(
    archive,
    folder,
    out_root,
    rate=32728,
    only=None,
    progress=None,
    should_cancel=None,
):
    """Rip one CSAR sound folder to WAV and manifest.csv."""
    from .formats.ctr import KIND_LABEL, cstm

    members = archive.folders[folder]
    todo = [s for s in members if only is None or s.index in only]
    out_dir = os.path.join(out_root, sanitize(folder))
    os.makedirs(out_dir, exist_ok=True)

    manifest = []
    counts = {'ok': 0, 'loop': 0, 'empty': 0, 'null': 0, 'error': 0}
    cancelled = False
    unapplied_before = dict(archive.unapplied)
    for done, sound in enumerate(todo, 1):
        res, chans, native_rate, loop = render_ctr_one(
            archive, sound, rate, keep_audio=False)
        counts[res.status] += 1
        if res.status != 'error':
            fn = sanitize(sound.name) + '.wav'
            cstm.write_wav(os.path.join(out_dir, fn), chans, native_rate, loop)
        peak = max((abs(x) for ch in (chans or []) for x in ch), default=0)
        manifest.append([
            sound.index, res.name, KIND_LABEL[sound.kind],
            round(res.duration, 3) if res.status != 'error' else '',
            peak if res.status != 'error' else '',
            round(res.loop_start, 3) if res.loop_start is not None else '',
            round(res.loop_end, 3) if res.loop_end is not None else '',
            res.status if res.status != 'error' else f'ERROR: {res.error}',
        ])
        if progress is not None:
            progress(done, len(todo), res)
        if should_cancel is not None and should_cancel():
            cancelled = True
            break

    with open(os.path.join(out_dir, 'manifest.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(CTR_MANIFEST_HEADER)
        w.writerows(manifest)

    note = None
    new_unapplied = {k: v - unapplied_before.get(k, 0)
        for k, v in archive.unapplied.items()
        if v > unapplied_before.get(k, 0)}
    if new_unapplied:
        note = ('parsed-but-unapplied commands (fidelity caveats): , '.join('%s x%d' % kv for kv in sorted(new_unapplied.items())))

    return {
        'arc_id': folder,
        'arc_name': folder,
        'out_dir': out_dir,
        'note': note,
        'cancelled': cancelled,
        'total': len(todo),
        **counts,
    }
