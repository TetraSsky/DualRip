"""DualRip command-line interface.

Same UX as the historical single-file script:
    dualrip --sdat sound_data.sdat --archive all --out SFX
"""

import argparse
import sys

from . import __version__
from .bankmap import parse_bank_map
from .export import rip_archive
from .formats.sdat import SdatFile


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='dualrip',
        description='Rip Nintendo DS SDAT sequence-archive (SSAR) sound '
        'effects to WAV, in raw form (one loop iteration, native '
        'silences, full releases, loop points in manifest + smpl '
        'chunk).',
    )
    ap.add_argument('--sdat', required=True, help='path to sound_data.sdat')
    ap.add_argument('--archive', default='all', help='SSAR index, or "all"')
    ap.add_argument('--out', default='DualRip_out', help='output directory')
    ap.add_argument('--rate', type=int, default=44100, help='output sample rate')
    ap.add_argument('--only', type=int, nargs='*', help='render only these entry indices')
    ap.add_argument(
        '--bank-map',
        default='',
        help='override bank resolution for NULL/dynamic slots, '
        'e.g. "4=32" or "4=32+33+43" (candidates tried in '
        'order, first full-coverage bank wins). Without this '
        'flag, resolution is automatic.',
    )
    ap.add_argument('--version', action='version', version=f'DualRip {__version__}')
    args = ap.parse_args(argv)

    sdat = SdatFile(args.sdat)
    override = parse_bank_map(args.bank_map)

    if args.archive == 'all':
        arc_ids = [i for i, _n, _c in sdat.seqarc_list]
    else:
        arc_ids = [int(args.archive)]

    only = set(args.only) if args.only else None
    for arc_id in arc_ids:
        seqarc = sdat.seqarc(arc_id)
        print(f'=== {seqarc.name}: {len(seqarc.entries)} entries')

        printed_note = [False]

        def progress(done, total, res):
            if not printed_note[0]:
                printed_note[0] = True
            if res.status == 'empty':
                print(f'  [{res.index:3d}] {res.name}: EMPTY')
            elif res.status == 'error':
                print(f'  [{res.index:3d}] {res.name}: ERROR {res.error}')
            elif res.index % 25 == 0:
                print(f'  [{res.index:3d}] {res.name}: {res.duration:.2f}s {res.status}')

        summary = rip_archive(
            sdat,
            arc_id,
            args.out,
            rate=args.rate,
            override_map=override,
            only=only,
            progress=progress,
        )
        if summary['note']:
            print(f"  note: {summary['note']} (--bank-map overrides this)")
        line = (
            f"  -> {summary['ok'] + summary['loop']} WAV written "
            f"({summary['loop']} looping), {summary['empty']} empty"
        )
        if summary['error']:
            line += f", {summary['error']} ERRORS (see manifest.csv)"
        print(line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
