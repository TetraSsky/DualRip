"""Layout constants shared by the NNS sound file formats (SWAR, SBNK).

Both files start with a 0x10-byte binary file header, an 8-byte DATA block
header and 0x20 reserved bytes; the record count and the record offset/entry
table follow at fixed positions.
"""

NNS_RECORD_COUNT_OFF = 0x38  # u32: number of records in the DATA block
NNS_RECORD_TABLE_OFF = 0x3C  # records (SWAR: u32 offsets; SBNK: entries)

# An SBNK references at most 4 wave archives; instruments address their
# sample as (wave archive slot, wave index).
BANK_WAVE_ARCHIVE_SLOTS = 4

# INFO-record value meaning "no wave archive in this slot".
NO_WAVE_ARCHIVE = 0xFFFF
