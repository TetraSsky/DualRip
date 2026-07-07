# Part of DualRip. Core playback logic is a faithful Python port of the FeOS
# Sound System (fincs), as adapted by Naram Qashat (CyberBotX) for the NCSF
# player (github.com/CyberBotX/in_xsf, src/in_ncsf/SSEQPlayer). Lookup tables
# come from disassembly of Nintendo's NNS sound driver by those authors.
# FIDELITY-CRITICAL: C integer semantics (truncating division, arithmetic
# shifts, table indexing) are intentional. Do not "simplify".

import csv
import os
import struct

import numpy as np

from .bankmap import BankResolver
from .engine.render import render_entry


def write_wav(path, stereo, rate, loop=None):
    """Minimal RIFF writer; embeds loop points as a standard `smpl` chunk."""
    data = stereo.tobytes()
    # fmt: PCM (1), stereo, rate, byte rate, block align (2ch x 16bit), bits
    fmt = struct.pack('<HHIIHH', 1, 2, rate, rate * 4, 4, 16)
    chunks = [(b'fmt ', fmt), (b'data', data)]
    if loop is not None:
        start, end = int(loop[0]), int(loop[1])
        end = max(end - 1, start)  # smpl dwEnd is the last sample included
        # smpl header: manufacturer, product, sample period (ns), MIDI unity
        # note (middle C), pitch fraction, SMPTE format/offset, 1 loop, 0 extra
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
    ):
        self.index = index
        self.name = name
        self.status = status  # ok | loop | empty | null | error
        self.bank_label = bank_label
        self.duration = duration
        self.loop_start = loop_start  # seconds, or None
        self.loop_end = loop_end
        self.audio = audio  # int16 stereo ndarray, or None
        self.error = error


def render_one(sdat, seqarc, entry, rate=44100, resolver=None):
    """Render one entry in memory (no I/O)."""
    if entry.offset is None:
        return RenderResult(entry.index, entry.name, 'null')
    if resolver is None:
        resolver = BankResolver(sdat, seqarc)
    try:
        rbid = resolver.resolve(entry)
        label = str(entry.bank_id) if rbid == entry.bank_id else f'{entry.bank_id}->{rbid}'
        bank, wave_arc = sdat.bank(rbid)
        audio, looped, marks = render_entry(
            seqarc.blob, entry.offset, bank, wave_arc, entry.volume, rate
        )
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
    except Exception as exc:  # keep ripping the rest of the archive
        return RenderResult(entry.index, entry.name, 'error', str(entry.bank_id), error=str(exc))


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
    """Rip one sequence archive to WAV + manifest.csv.

    progress(done, total, RenderResult) called per entry;
    should_cancel() -> True aborts cleanly.
    """
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
        manifest.append(
            [
                entry.index,
                res.name,
                res.bank_label,
                '' if entry.volume is None else entry.volume,
                (
                    round(res.duration, 3)
                    if res.status in ('ok', 'loop')
                    else (0.0 if res.status == 'empty' else '')
                ),
                round(res.loop_start, 3) if res.loop_start is not None else '',
                round(res.loop_end, 3) if res.loop_end is not None else '',
                res.status if res.status != 'error' else f'ERROR: {res.error}',
            ]
        )
        res.audio = None  # free memory during long batches
        if progress is not None:
            progress(done, len(todo), res)
        if should_cancel is not None and should_cancel():
            cancelled = True
            break

    with open(os.path.join(out_dir, 'manifest.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(
            [
                'index',
                'name',
                'bank',
                'entry_volume',
                'duration_s',
                'loop_start_s',
                'loop_end_s',
                'status',
            ]
        )
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
