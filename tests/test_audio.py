import numpy as np

from adk_agent import twilio_stream


def test_decode_matches_g711_reference_points():
    pcm = twilio_stream.ulaw_to_pcm16(bytes([0xFF, 0x7F, 0x80, 0x00]))
    assert pcm.tolist() == [0, 0, 32124, -32124]


def test_every_ulaw_byte_survives_round_trip():
    ulaw = bytes(range(256))
    round_trip = twilio_stream.pcm16_to_ulaw(twilio_stream.ulaw_to_pcm16(ulaw))
    # 0x7F and 0xFF both decode to 0, which encodes back to 0xFF.
    expected = bytes(0xFF if b == 0x7F else b for b in ulaw)
    assert round_trip == expected


def test_encode_error_is_within_quantisation_step():
    pcm = np.linspace(-32768, 32767, 5000).astype(np.int16)
    decoded = twilio_stream.ulaw_to_pcm16(twilio_stream.pcm16_to_ulaw(pcm)).astype(np.int32)
    error = np.abs(decoded - pcm.astype(np.int32))
    # Largest μ-law step is 1024; clipping at ±32635 adds a little at the extremes.
    assert error.max() <= 1024 + 133
