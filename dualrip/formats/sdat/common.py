"""Shared NNS record-table offsets for SWAR and SBNK."""

# 0x10 header, 8-byte DATA header, 0x20 reserved, then u32 count and record table
NNS_RECORD_COUNT_OFF = 0x38
NNS_RECORD_TABLE_OFF = 0x3C # SWAR: u32 offsets, SBNK: entries

BANK_WAVE_ARCHIVE_SLOTS = 4 # max wave archives per SBNK
NO_WAVE_ARCHIVE = 0xFFFF # unused slot
