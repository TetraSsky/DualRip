"""DualRip -- Nintendo DS SDAT sound-effect (SSAR) ripper."""

__version__ = '2.0.0'

from .bankmap import BankResolver, parse_bank_map  # noqa: F401
from .export import RenderResult, render_one, rip_archive  # noqa: F401
from .formats.sdat import SdatFile  # noqa: F401
