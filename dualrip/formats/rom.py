"""
NDS ROM adapter: extract SDAT files from a Nintendo DS ROM (.nds).

Uses ndspy.rom.NintendoDSRom to open the ROM and scans for files whose magic
bytes are b'SDAT'.  Returns raw bytes so the SDAT layer (SdatFile.from_bytes)
can parse them without touching the disk.

Games with multiple SDATs are handled upstream this module simply reports what is available.
"""

from __future__ import annotations

import ndspy.rom
import ndspy.soundArchive


def _quick_stats(sdat_data: bytes) -> dict:
    """Lightweight SDAT header peek (no full parse of banks/waves)."""
    sdat = ndspy.soundArchive.SDAT(sdat_data)
    return {
        'seqarcs': sum(1 for _, a in sdat.sequenceArchives if a is not None),
        'sseqs': sum(1 for _, s in sdat.sequences if s is not None),
        'banks': sum(1 for _, b in sdat.banks if b is not None),
        'swars': sum(1 for _, w in sdat.waveArchives if w is not None),
    }


def find_sdats_in_rom(nds_path: str) -> list[dict]:
    """
    Open a .nds ROM and return a list of SDAT descriptors.

    Each descriptor is:
        {
            'index': int, # file index inside rom.files
            'size': int, # raw byte count
            'data': bytes, # SDAT bytes, ready for SdatFile.from_bytes
            'seqarcs': int, # number of sequence archives
            'sseqs': int, # number of standalone SSEQ (music)
            'banks': int, # number of banks
            'swars': int, # number of wave archives
        }

    Raises FileNotFoundError if the path does not exist.
    Raises ValueError if the ROM contains zero SDAT files.
    """
    rom = ndspy.rom.NintendoDSRom.fromFile(nds_path)
    found = []
    for idx, data in enumerate(rom.files):
        if len(data) >= 4 and bytes(data[:4]) == b'SDAT':
            raw = bytes(data)
            stats = _quick_stats(raw)
            found.append({
                'index': idx,
                'size': len(raw),
                'data': raw,
                **stats,
            })
    if not found:
        raise ValueError(f'No SDAT file found in {nds_path!r}')
    return found
