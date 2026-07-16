"""Extract SDAT files from a .nds ROM."""

from __future__ import annotations

import ndspy.rom
import ndspy.soundArchive

def _quick_stats(sdat_data: bytes) -> dict:
    """SDAT header counts without a full parse."""
    sdat = ndspy.soundArchive.SDAT(sdat_data)
    return {
        'seqarcs': sum(1 for _, a in sdat.sequenceArchives if a is not None),
        'sseqs': sum(1 for _, s in sdat.sequences if s is not None),
        'banks': sum(1 for _, b in sdat.banks if b is not None),
        'swars': sum(1 for _, w in sdat.waveArchives if w is not None),
    }

def find_sdats_in_rom(nds_path: str) -> list[dict]:
    """SDAT descriptors found in a .nds ROM, one per SDAT file."""
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
