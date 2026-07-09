"""
DualRip command-line interface.

Same UX as the historical single-file script:
    dualrip --sdat sound_data.sdat --archive all --out SFX
    dualrip --sdat sound_data.sdat --sequence all --out MUSIC
    dualrip --sdat game.nds --sdat-index 0 --archive all --out SFX
"""

import argparse
import os
import sys
from . import __version__
from .bankmap import parse_bank_map
from .export import rip_archive, rip_sequences
from .formats.rom import find_sdats_in_rom
from .formats.sdat import SdatFile

def _print_summary(summary):
    if summary['note']:
        print(f"note: {summary['note']} (--bank-map overrides this)")
    line = (
        f"-> {summary['ok'] + summary['loop']} WAV written "
        f"({summary['loop']} looping), {summary['empty']} empty"
    )
    if summary['error']:
        line += f", {summary['error']} ERRORS (see manifest.csv)"
    print(line)

def _progress(done, total, res):
    if res.status == 'empty':
        print(f'[{res.index:3d}] {res.name}: EMPTY')
    elif res.status == 'error':
        print(f'[{res.index:3d}] {res.name}: ERROR {res.error}')
    elif res.index % 25 == 0:
        print(f'[{res.index:3d}] {res.name}: {res.duration:.2f}s {res.status}')

def main(argv=None):
    ap = argparse.ArgumentParser(prog='dualrip', description='Rip Nintendo DS SDAT (SSAR + SSEQ) to WAV — raw export, one loop iteration, loop points in manifest + smpl chunk.',)
    ap.add_argument('--sdat', required=True, help='path to sound_data.sdat, or a .nds ROM (use --sdat-index to pick the SDAT when multiple exist)',)
    ap.add_argument('--sdat-index', type=int, default=None, metavar='N', help='index of the SDAT inside a .nds ROM (0=first). If omitted and the ROM has multiple SDATs, they are listed and the program exits.',)
    ap.add_argument('--archive', default=None, help='SSAR index, or "all" (sound effects). Default "all" when --sequence is not given',)
    ap.add_argument('--sequence', nargs='+', default=None, metavar='N', help='SSEQ indices, or "all" (music). Extracts music into an SSEQ/ subfolder',)
    ap.add_argument('--out', default='DualRip_out', help='output directory')
    ap.add_argument('--rate', type=int, default=44100, help='output sample rate')
    ap.add_argument('--only', type=int, nargs='*', help='render only these entry indices')
    ap.add_argument('--bank-map', default='', help='override bank resolution for NULL/dynamic slots, e.g. "4=32" or "4=32+33+43" (candidates tried in order, first full-coverage bank wins). Without this flag, resolution is automatic.',)
    ap.add_argument('--version', action='version', version=f'DualRip {__version__}')
    args = ap.parse_args(argv)

    if args.sdat.lower().endswith('.nds'):
        sdats = find_sdats_in_rom(args.sdat)
        if args.sdat_index is not None:
            if not 0 <= args.sdat_index < len(sdats):
                print(f'error: --sdat-index {args.sdat_index} out of range (0-{len(sdats)-1})')
                return 1
            chosen = sdats[args.sdat_index]
        elif len(sdats) == 1:
            chosen = sdats[0]
        else:
            print(f'{os.path.basename(args.sdat)} contains {len(sdats)} SDAT files. Use --sdat-index to pick one:')
            for i, s in enumerate(sdats):
                size_kb = s['size'] / 1024
                print(f'  [{i}] {size_kb:.0f} KB — {s["seqarcs"]} SSAR, {s["sseqs"]} SSEQ, {s["banks"]} banks, {s["swars"]} SWAR')
            return 1
        label = os.path.basename(args.sdat)
        if len(sdats) > 1:
            label += f' [SDAT #{chosen["index"]}]'
        sdat = SdatFile.from_bytes(chosen['data'], label=label)
        print(f'Using SDAT #{chosen["index"]} ({chosen["size"]/1024:.0f} KB) from {os.path.basename(args.sdat)}')
    else:
        sdat = SdatFile(args.sdat)

    override = parse_bank_map(args.bank_map)

    archive_sel = args.archive
    if archive_sel is None and args.sequence is None:
        archive_sel = 'all'

    if archive_sel == 'all':
        arc_ids = [i for i, _n, _c in sdat.seqarc_list]
    elif archive_sel is not None:
        arc_ids = [int(archive_sel)]
    else:
        arc_ids = []

    only = set(args.only) if args.only else None
    for arc_id in arc_ids:
        seqarc = sdat.seqarc(arc_id)
        print(f'=== {seqarc.name}: {len(seqarc.entries)} entries')
        _print_summary(
            rip_archive(
                sdat,
                arc_id,
                args.out,
                rate=args.rate,
                override_map=override,
                only=only,
                progress=_progress,
            )
        )

    if args.sequence is not None:
        if len(args.sequence) == 1 and args.sequence[0] == 'all':
            seq_ids = [sid for sid, _n, _b in sdat.sequence_list]
        else:
            seq_ids = [int(x) for x in args.sequence]
        print(f'=== SSEQ (music): {len(seq_ids)} sequences')
        _print_summary(
            rip_sequences(
                sdat,
                seq_ids,
                args.out,
                rate=args.rate,
                override_map=override,
                progress=_progress,
            )
        )
    return 0

if __name__ == '__main__':
    sys.exit(main())
