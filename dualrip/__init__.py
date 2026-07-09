"""
DualRip -- Nintendo DS SDAT sound-effect (SSAR & SSEQ) ripper.
"""

__version__ = '2.1.1'

from .bankmap import BankResolver, parse_bank_map
from .export import (
    RenderResult,
    render_one,
    rip_archive,
    rip_sequences,
)
from .formats.sdat import SdatFile
