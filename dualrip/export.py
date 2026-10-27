"""WAV export and batch rip."""

import csv
import os
import struct
import numpy as np
from .bankmap import BankResolver, empty_reason
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

def write_wav_channels(path, audio, rate, loop=None):
    """RIFF writer for an (n, channels) array, keeping the source channel count."""
    channels = audio.shape[1] if audio.ndim > 1 else 1
    block = channels * 2
    fmt = struct.pack('<HHIIHH', 1, channels, rate, rate * block, block, 16)
    chunks = [(b'fmt ', fmt), (b'data', audio.tobytes())]
    if loop is not None:
        start, end = int(loop[0]), int(loop[1])
        end = max(end - 1, start) # smpl dwEnd is the last sample included
        smpl = struct.pack('<9I', 0, 0, 1000000000 // rate, 60, 0, 0, 0, 1, 0)
        smpl += struct.pack('<6I', 0, 0, start, end, 0, 0)
        chunks.append((b'smpl', smpl))
    payload = b''
    for cid, cdata in chunks:
        payload += cid + struct.pack('<I', len(cdata)) + cdata
        if len(cdata) & 1:
            payload += b'\x00'
    with open(path, 'wb') as f:
        f.write(b'RIFF' + struct.pack('<I', 4 + len(payload)) + b'WAVE' + payload)

def _to_stereo(audio):
    """An (n, channels) array as stereo, mono duplicated to both sides."""
    if audio.ndim == 1:
        return np.column_stack((audio, audio))
    if audio.shape[1] == 1:
        return np.column_stack((audio[:, 0], audio[:, 0]))
    return np.ascontiguousarray(audio[:, :2])

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
        'reason',
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
        reason='',
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
        self.reason = reason # why an entry rendered empty (*if known)

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
        refusals = {}
        audio, looped, marks = render_entry(seqarc.blob, entry.offset, bank, wave_arc, entry.volume, rate, player_prio=entry.cpr or 0, loop_passes=2, refusals=refusals)
        peak = int(np.abs(audio.astype(np.int32)).max()) if len(audio) else 0
        if len(audio) == 0 or peak == 0:
            return RenderResult(entry.index, entry.name, 'empty', label, reason=empty_reason(seqarc.blob, entry, refusals))
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

def _peak(res):
    """Highest absolute sample value of a render, 0 when it kept no audio."""
    if res.audio is None or not len(res.audio):
        return 0
    return int(np.abs(res.audio.astype(np.int32)).max())

def manifest_row(index, res, kind='', bank='', entry_volume=None, peak=0):
    """One manifest.csv row, shared by the SSAR, SSEQ, CSAR and DSE rippers."""
    return [
        index,
        res.name,
        kind,
        bank,
        '' if entry_volume is None else entry_volume,
        (
            round(res.duration, 3)
            if res.status in ('ok', 'loop')
            else (0.0 if res.status == 'empty' else '')
        ),
        peak if res.status != 'error' else '',
        round(res.loop_start, 3) if res.loop_start is not None else '',
        round(res.loop_end, 3) if res.loop_end is not None else '',
        res.status if res.status != 'error' else f'ERROR: {res.error}',
    ]

MANIFEST_HEADER = [
    'index',
    'name',
    'kind',
    'bank',
    'entry_volume',
    'duration_s',
    'peak',
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
        manifest.append(manifest_row(sid, res, 'seq', res.bank_label, entry.volume, _peak(res)))
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
        manifest.append(manifest_row(entry.index, res, 'sfx', res.bank_label, entry.volume, _peak(res)))
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

def _ctr_empty_reason(sound, stats):
    """CSAR sound rendered empty: bank rejections, no notes, or a zero entry volume"""
    if stats.get('body empty'):
        return 'unused slot'
    notes = stats.get('notes', 0)
    if notes and notes == stats.get('rejected', 0):
        return 'notes rejected by bank'
    if sound.volume == 0:
        return 'entry volume zero'
    return ''

def render_ctr_one(archive, sound, rate, keep_audio=True, layer=None):
    """Render one CSAR sound in memory or one stereo layer of a multi-channel stream."""
    from .formats.ctr import KIND_LABEL
    stats = {}
    try:
        native_rate, chans, loop = archive.render(sound, rate, stats, layer=layer)
    except (LookupError, ValueError, NotImplementedError) as exc:
        return (RenderResult(sound.index, sound.name, 'error', error=str(exc)), None, None, None)
    n = len(chans[0]) if chans else 0
    peak = max((abs(x) for ch in chans for x in ch), default=0)
    ls = loop[0] / native_rate if loop else None
    le = loop[1] / native_rate if loop else None
    status = 'empty' if (n == 0 or peak == 0) else ('loop' if loop else 'ok')
    res = RenderResult(sound.index, sound.name, status, KIND_LABEL[sound.kind], n / native_rate if n else 0.0, ls, le, _ctr_chans_to_stereo(chans) if keep_audio else None, rate=native_rate, reason=_ctr_empty_reason(sound, stats) if status == 'empty' else '')
    return res, chans, native_rate, loop

def _ctr_bank_cell(sound):
    """Bank ids a CSAR sound references, empty when it references none."""
    ids = [b for b in getattr(sound, 'bank_ids', []) if b != 0xFFFFFF]
    return ', '.join(str(b) for b in ids)

def rip_ctr_folder(
    archive,
    folder,
    out_root,
    rate=32728,
    only=None,
    layers=None,
    stream_mode='multiple',
    progress=None,
    should_cancel=None,
):
    """Rip one CSAR sound folder to WAV and manifest.csv (stream_mode: multiple, single or both)."""
    from .formats.ctr import KIND_LABEL, cstm

    members = archive.folders[folder]
    todo = [s for s in members if only is None or s.index in only]
    out_dir = os.path.join(out_root, sanitize(folder))
    os.makedirs(out_dir, exist_ok=True)

    manifest = []
    counts = {'ok': 0, 'loop': 0, 'empty': 0, 'null': 0, 'error': 0}
    cancelled = False
    unapplied_before = dict(archive.unapplied)
    mode = stream_mode or 'multiple'

    def stream_files(sound):
        """Files one stream produces under the chosen mode."""
        n = archive.stream_layers(sound) if KIND_LABEL[sound.kind] == 'strm' else 1
        if n <= 1:
            return 1
        wanted = (layers or {}).get(sound.index)
        n = len(wanted) if wanted else n
        return n if mode == 'multiple' else (1 if mode == 'single' else n + 1)

    total_files = sum(stream_files(s) for s in todo)
    written = 0
    for done, sound in enumerate(todo, 1):
        res, chans, native_rate, loop = render_ctr_one(
            archive, sound, rate, keep_audio=False)
        files = 0
        if res.status != 'error':
            wanted = (layers or {}).get(sound.index)
            total_layers = archive.stream_layers(sound) if KIND_LABEL[sound.kind] == 'strm' else 1
            if total_layers > 1:
                # multichannel file keeps every layer in channel order
                if mode in ('single', 'both'):
                    fn = sanitize(sound.name) + '.wav'
                    cstm.write_wav(os.path.join(out_dir, fn), chans, native_rate, loop)
                    peak = max((abs(x) for ch in (chans or []) for x in ch), default=0)
                    row = manifest_row(
                        sound.index, res, KIND_LABEL[sound.kind], _ctr_bank_cell(sound),
                        sound.volume, peak)
                    row[1] = f'{sound.name} ({total_layers * 2} ch)'
                    manifest.append(row)
                    counts[res.status] += 1
                    files += 1
                    written += 1
                    if progress is not None:
                        progress(written, total_files, res)
                if mode in ('multiple', 'both'):
                    for k in (sorted(wanted) if wanted else range(total_layers)):
                        _res, pair, pair_rate, pair_loop = render_ctr_one(archive, sound, rate, keep_audio=False, layer=k)
                        fn = f'{sanitize(sound.name)}_c{2 * k}-{2 * k + 1}.wav'
                        cstm.write_wav(os.path.join(out_dir, fn), pair, pair_rate, pair_loop)
                        peak = max((abs(x) for ch in (pair or []) for x in ch), default=0)
                        row = manifest_row(sound.index, _res, KIND_LABEL[sound.kind], _ctr_bank_cell(sound),sound.volume, peak)
                        row[1] = f'{sound.name} (ch {2 * k}-{2 * k + 1})'
                        manifest.append(row)
                        counts[_res.status] += 1
                        files += 1
                        written += 1
                        if progress is not None:
                            progress(written, total_files, _res)
            else:
                fn = sanitize(sound.name) + '.wav'
                cstm.write_wav(os.path.join(out_dir, fn), chans, native_rate, loop)
                peak = max((abs(x) for ch in (chans or []) for x in ch), default=0)
                manifest.append(manifest_row(
                    sound.index, res, KIND_LABEL[sound.kind], _ctr_bank_cell(sound),
                    sound.volume, peak))
                counts[res.status] += 1
                files = 1
                written += 1
        if not files:
            counts[res.status] += 1
            if progress is not None:
                progress(written, total_files, res)
        if should_cancel is not None and should_cancel():
            cancelled = True
            break

    with open(os.path.join(out_dir, 'manifest.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(MANIFEST_HEADER)
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

def render_strm_one(sdat, stream_id, name, keep_audio=True):
    """Render one SDAT streamed sound in memory, at its own encoded rate."""
    try:
        strm = sdat.stream(stream_id)
        audio = strm.decode()
    except (LookupError, ValueError, IndexError, NotImplementedError) as exc:
        return RenderResult(stream_id, name, 'error', error=str(exc)), None, None, None
    native_rate = strm.sample_rate
    loop = strm.loop_points()
    n = len(audio)
    peak = int(np.abs(audio.astype(np.int32)).max()) if n else 0
    ls = loop[0] / native_rate if loop else None
    le = loop[1] / native_rate if loop else None
    status = 'empty' if (n == 0 or peak == 0) else ('loop' if loop else 'ok')
    res = RenderResult(stream_id, name, status, 'strm', n / native_rate if n else 0.0, ls, le, _to_stereo(audio) if keep_audio else None, rate=native_rate)
    return res, audio, native_rate, loop

def rip_streams(sdat, stream_ids, out_root, progress=None, should_cancel=None):
    """Rip SDAT streamed sounds to WAV and manifest.csv, each at its native rate."""
    out_dir = os.path.join(out_root, 'STRM')
    os.makedirs(out_dir, exist_ok=True)
    names = {i: n for i, n, _v in sdat.stream_list}
    volumes = {i: v for i, _n, v in sdat.stream_list}

    manifest = []
    counts = {'ok': 0, 'loop': 0, 'empty': 0, 'null': 0, 'error': 0}
    cancelled = False
    for done, sid in enumerate(stream_ids, 1):
        name = names.get(sid, f'STRM_{sid}')
        res, audio, native_rate, loop = render_strm_one(sdat, sid, name)
        counts[res.status] += 1
        if res.status != 'error':
            fn = f'{sid:03d}_{sanitize(name)}.wav'
            write_wav_channels(os.path.join(out_dir, fn), audio, native_rate, loop)
        manifest.append(manifest_row(sid, res, 'strm', '', volumes.get(sid), _peak(res)))
        res.audio = None # free memory during long batches
        if progress is not None:
            progress(done, len(stream_ids), res)
        if should_cancel is not None and should_cancel():
            cancelled = True
            break

    with open(os.path.join(out_dir, 'manifest.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(MANIFEST_HEADER)
        w.writerows(manifest)

    return {
        'arc_id': 'STRM',
        'arc_name': 'STRM (streams)',
        'out_dir': out_dir,
        'note': None,
        'cancelled': cancelled,
        'total': len(stream_ids),
        **counts,
    }

def _dse_empty_reason(entry, stats):
    """DSE sound rendered empty because the bank rejected all notes (or had none)"""
    if stats:
        return max(stats, key=stats.get)
    if entry.events and not any(e[1] == 'on' for e in entry.events):
        return 'no notes'
    return ''

def render_dse_one(archive, entry, rate, keep_audio=True):
    """Render one DSE sound in memory. Streams carry their own native rate and loop information."""
    from .formats.dse import KIND_LABEL
    stats = {}
    try:
        audio, out_rate, loop = archive.render(entry, rate, stats)
    except (LookupError, ValueError, KeyError, IndexError, NotImplementedError) as exc:
        return RenderResult(entry.index, entry.name, 'error', error=str(exc)), None, None, None
    n = len(audio)
    peak = int(np.abs(audio.astype(np.int32)).max()) if n else 0
    ls = loop[0] / out_rate if loop else None
    le = loop[1] / out_rate if loop else None
    status = 'empty' if (n == 0 or peak == 0) else ('loop' if loop else 'ok')
    res = RenderResult(entry.index, entry.name, status, KIND_LABEL[entry.kind], n / out_rate if n else 0.0, ls, le, audio if keep_audio else None, rate=out_rate, reason=_dse_empty_reason(entry, stats) if status == 'empty' else '')
    return res, audio, out_rate, loop

def rip_dse_folder(archive, folder, out_root, rate=32728, only=None, progress=None, should_cancel=None):
    """Rip one folder of DSE sounds to WAV and manifest.csv."""
    members = archive.folders[folder]
    todo = [s for s in members if only is None or s.index in only]
    out_dir = os.path.join(out_root, sanitize(folder))
    os.makedirs(out_dir, exist_ok=True)

    manifest = []
    counts = {'ok': 0, 'loop': 0, 'empty': 0, 'null': 0, 'error': 0}
    cancelled = False
    seen = set() # two ROM members can share a name, keep both files
    for done, entry in enumerate(todo, 1):
        res, audio, out_rate, loop = render_dse_one(archive, entry, rate, keep_audio=False)
        counts[res.status] += 1
        if res.status != 'error':
            name = sanitize(entry.name)
            if name in seen:
                name = '%s_%d' % (name, entry.index)
            seen.add(name)
            write_wav(os.path.join(out_dir, name + '.wav'), audio, out_rate, loop)
        peak = int(np.abs(audio.astype(np.int32)).max()) if audio is not None and len(audio) else 0
        manifest.append(manifest_row(
            entry.index, res, entry.kind, entry.bank_name or '', None, peak))
        if progress is not None:
            progress(done, len(todo), res)
        if should_cancel is not None and should_cancel():
            cancelled = True
            break

    with open(os.path.join(out_dir, 'manifest.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(MANIFEST_HEADER)
        w.writerows(manifest)

    return {'arc_id': folder, 'arc_name': folder, 'out_dir': out_dir, 'note': None, 'cancelled': cancelled, 'total': len(todo), **counts}
