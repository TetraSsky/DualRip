"""
Shared NNS layout offsets (SWAR, SBNK).

File layout: 0x10-byte header | 8-byte DATA block header | 0x20 reserved | u32 record count | record table.
"""

NNS_RECORD_COUNT_OFF = 0x38 # u32: record count in DATA block
NNS_RECORD_TABLE_OFF = 0x3C # record table (SWAR: u32 offsets; SBNK: entries)

BANK_WAVE_ARCHIVE_SLOTS = 4 # max wave archives per SBNK
NO_WAVE_ARCHIVE = 0xFFFF # sentinel: slot unused
