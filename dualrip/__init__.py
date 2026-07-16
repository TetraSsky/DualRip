"""Nintendo DS and 3DS audio ripper."""

from ._version import __version__

from .bankmap import BankResolver, parse_bank_map
from .export import (
    RenderResult,
    render_one,
    rip_archive,
    rip_sequences,
)
from .formats.sdat import SdatFile
