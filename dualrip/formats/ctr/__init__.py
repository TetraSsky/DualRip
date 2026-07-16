"""Nintendo 3DS audio format parsing."""

from .archive import CtrArchive, find_csars_in_rom, open_bcsar, KIND_LABEL, KIND_TITLE
from .rom import Boot9RequiredError
