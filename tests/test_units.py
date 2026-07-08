"""
Unit tests for the fidelity-critical primitives (no game data needed).
"""

from dualrip.cprims import (
    cdiv,
    cnv_attack,
    cnv_fall,
    cnv_scale,
    cnv_sust,
    muldiv7,
    s8,
    s16,
    timer_adjust,
)
from dualrip.formats.swar import decode_adpcm
from dualrip.tables import GETPITCHTBL, GETVOLTBL


def test_cdiv_truncates_toward_zero():
    # C semantics, NOT Python floor division
    assert cdiv(-7, 2) == -3
    assert cdiv(7, -2) == -3
    assert cdiv(7, 2) == 3
    assert cdiv(-92544 * 255, 256) == -92182 # attack envelope first step


def test_sign_casts():
    assert s8(0xF4) == -12 # real SSEQ TRANSPOSE byte (244, wraps to -12)
    assert s16(0xFB40) == -1216 # tie sweep glide


def test_muldiv7_passthrough_at_127():
    assert muldiv7(12345, 127) == 12345
    assert muldiv7(-32768, 64) == -16384


def test_envelope_conversions():
    assert cnv_attack(127) == 0 # instant attack
    assert cnv_attack(0) == 0xFF # slowest
    assert cnv_fall(0x7F) == 0xFFFF
    assert cnv_fall(0x7E) == 0x3C00
    assert cnv_sust(127) == 0
    assert cnv_sust(0) == -32768
    assert cnv_scale(127) == 0


def test_tables_shape():
    assert len(GETPITCHTBL) == 768 # 12 semitones * 64 steps
    assert len(GETVOLTBL) == 724 # AMPL_K + 1


def test_timer_adjust_identity_and_octave():
    # pitch 0 -> unchanged; +768 (one octave up) -> timer halves
    assert timer_adjust(0x4000, 0) == 0x4000
    assert timer_adjust(0x4000, 768) == 0x2000
    assert timer_adjust(0x4000, -768) == 0x8000


def test_adpcm_header_and_clamp():
    # initial predictor is emitted as-is for a zero nibble stream
    raw = bytes([0x00, 0x10, 0x00, 0x00]) + bytes([0x00] * 4)
    out = decode_adpcm(raw)
    assert len(out) == 8
    assert out.max() <= 0x7FFF and out.min() >= -0x8000
