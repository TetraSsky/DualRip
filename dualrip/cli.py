"""Command-line interface."""

import argparse
import os
import sys
from . import __version__
from .bankmap import parse_bank_map
from .export import rip_archive, rip_ctr_folder, rip_sequences
from .formats.ctr import Boot9RequiredError, find_csars_in_rom, open_bcsar
from .formats.sdat import SdatFile, find_sdats_in_rom

def _print_summary(summary, note_hint=''):
    if summary['note']:
        print(f"note: {summary['note']}{note_hint}")
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

def _run_nds(args):
    if args.folder is not None:
        print('warning: --folder has no effect on NDS input, ignored (use --archive/--sequence)')

    if args.file.lower().endswith('.nds'):
        sdats = find_sdats_in_rom(args.file)
        if args.archive_index is not None:
            if not 0 <= args.archive_index < len(sdats):
                print(f'error: --archive-index {args.archive_index} out of range (0-{len(sdats)-1})')
                return 1
            chosen = sdats[args.archive_index]
        elif len(sdats) == 1:
            chosen = sdats[0]
        else:
            print(f'{os.path.basename(args.file)} contains {len(sdats)} SDAT files. Use --archive-index to pick one:')
            for i, s in enumerate(sdats):
                size_kb = s['size'] / 1024
                print(f'[{i}] {size_kb:.0f} KB — {s["seqarcs"]} SSAR, {s["sseqs"]} SSEQ, {s["banks"]} banks, {s["swars"]} SWAR')
            return 1
        label = os.path.basename(args.file)
        if len(sdats) > 1:
            label += f'[SDAT #{chosen["index"]}]'
        sdat = SdatFile.from_bytes(chosen['data'], label=label)
        print(f'Using SDAT #{chosen["index"]} ({chosen["size"]/1024:.0f} KB) from {os.path.basename(args.file)}')
    else:
        sdat = SdatFile(args.file)

    if args.list:
        for i, name, count in sdat.seqarc_list:
            print(f'[{i:3d}] {name}  ({count} entries)')
        if sdat.sequence_list:
            print(f'SSEQ (music): {len(sdat.sequence_list)} sequences')
        return 0

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
            ),
            note_hint=' (--bank-map overrides this)',
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
            ),
            note_hint=' (--bank-map overrides this)',
        )
    return 0

def _open_ctr(path, index, boot9=None):
    """Open a 3DS ROM or loose .bcsar, returns one CtrArchive or None."""
    if path.lower().endswith('.bcsar'):
        return open_bcsar(path)
    archives = find_csars_in_rom(path, boot9=boot9)
    if index is not None:
        if not 0 <= index < len(archives):
            print(f'error: --archive-index {index} out of range (0-{len(archives)-1})')
            return None
        return archives[index]
    if len(archives) == 1:
        return archives[0]
    print(f'{os.path.basename(path)} contains {len(archives)} CSAR sound archives. Use --archive-index to pick one:')
    for i, arch in enumerate(archives):
        c = arch.counts()
        print(f'[{i}] {arch.label} — {c["seq"]} CSEQ, {c["wsd"]} CWSD, {c["strm"]} BCSTM, {len(arch.csar.banks)} CBNK, {len(arch.csar.wars)} CWAR')
    return None

def _run_ctr(args):
    if args.archive is not None or args.sequence is not None:
        print('warning: --archive/--sequence have no effect on 3DS input, ignored (use --folder)')
    if args.bank_map:
        print('warning: --bank-map has no effect on 3DS input, ignored')

    try:
        archive = _open_ctr(args.file, args.archive_index, boot9=args.boot9)
    except Boot9RequiredError:
        print(f'error: {os.path.basename(args.file)} is encrypted. Provide the console boot ROM dump with --boot9 PATH.')
        return 1
    if archive is None:
        return 1

    if args.list:
        print(f'{archive.label}:')
        for folder, members in archive.folders.items():
            print(f'{folder}  ({len(members)} sounds)')
        return 0

    folder_sel = args.folder or ['all']
    if len(folder_sel) == 1 and folder_sel[0] == 'all':
        folders = list(archive.folders)
    else:
        unknown = [f for f in folder_sel if f not in archive.folders]
        if unknown:
            print(f'error: unknown folder(s): {", ".join(unknown)} (see --list)')
            return 1
        folders = folder_sel

    only = set(args.only) if args.only else None
    for folder in folders:
        members = archive.folders[folder]
        print(f'=== {folder}: {len(members)} sounds')
        _print_summary(rip_ctr_folder(archive, folder, args.out, rate=args.rate, only=only, progress=_progress))
    return 0

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='dualrip',
        description='Rip Nintendo DS SDAT (SSAR + SSEQ) or 3DS CSAR sounds to WAV — raw export, steady-state loop (2 passes), loop points in manifest + smpl chunk.',
    )
    ap.add_argument('--file', required=True, help='path to sound_data.sdat, a .nds ROM, a 3DS ROM (.cia/.3ds) or a .bcsar sound archive (use --archive-index to pick when it holds multiple archives)',)
    ap.add_argument('--archive-index', type=int, default=None, metavar='N', help='index of the SDAT/CSAR inside a multi-archive file (0=first). If omitted and there are several, they are listed and the program exits.',)
    ap.add_argument('--archive', default=None, help='NDS: SSAR index, or "all" (sound effects). Default "all" when --sequence is not given',)
    ap.add_argument('--sequence', nargs='+', default=None, metavar='N', help='NDS: SSEQ indices, or "all" (music). Extracts music into an SSEQ/ subfolder',)
    ap.add_argument('--folder', nargs='+', default=None, metavar='NAME', help='3DS: CSAR folder name(s) (see --list), or "all" (default)',)
    ap.add_argument('--list', action='store_true', help='list archives/folders and exit without ripping')
    ap.add_argument('--out', default='DualRip_out', help='output directory')
    ap.add_argument('--rate', type=int, default=44100, help='output sample rate')
    ap.add_argument('--only', type=int, nargs='*', help='render only these entry/sound indices')
    ap.add_argument('--bank-map', default='', help='NDS only: override bank resolution for NULL/dynamic slots, e.g. "4=32" or "4=32+33+43" (candidates tried in order, first full-coverage bank wins). Without this flag, resolution is automatic.',)
    ap.add_argument('--boot9', default=None, metavar='PATH', help='3DS only: console boot ROM dump (boot9.bin), needed to open an encrypted .cia/.3ds. Decrypted ROMs open without it.',)
    ap.add_argument('--version', action='version', version=f'DualRip {__version__}')
    args = ap.parse_args(argv)

    if args.file.lower().endswith(('.cia', '.3ds', '.bcsar')):
        return _run_ctr(args)
    return _run_nds(args)

if __name__ == '__main__':
    sys.exit(main())
