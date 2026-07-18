import importlib.util
from pathlib import Path


ROOT = Path(__file__).parents[1]
DRIVER = ROOT / "main/lib/fw1_radio_decode.py"
spec = importlib.util.spec_from_file_location("fw1_radio_decode", DRIVER)
decode = importlib.util.module_from_spec(spec)
spec.loader.exec_module(decode)


def princeton(key, bits=24, te=350):
    values = []
    for shift in range(bits - 1, -1, -1):
        if (key >> shift) & 1:
            values.extend((te * 3, -te))
        else:
            values.extend((te, -te * 3))
    values.extend((te, -te * 31))
    return values


def test_decodes_repeated_princeton_key():
    timings = princeton(0xA1B2C3) + princeton(0xA1B2C3)
    frames = decode.decode_princeton_frames(timings)
    assert len(frames) == 2
    assert all(frame["key"] == 0xA1B2C3 for frame in frames)
    assert all(frame["bits"] == 24 for frame in frames)
    assert all(frame["te_us"] == 350 for frame in frames)


def test_tolerates_demodulator_timing_jitter():
    timings = princeton(0x123456)
    for index, value in enumerate(timings):
        if index % 7 == 0:
            timings[index] = value + (25 if value > 0 else -25)
    frame = decode.decode_princeton_frames(timings)[0]
    assert frame["key"] == 0x123456
    assert 350 <= frame["te_us"] <= 375


def test_ignores_partial_or_malformed_frames():
    assert decode.decode_princeton_frames(princeton(0xA1B2C3)[:30]) == ()
    malformed = princeton(0xA1B2C3)
    malformed[10] = 5_000
    assert decode.decode_princeton_frames(malformed) == ()


def test_rejects_unbounded_bit_count():
    try:
        decode.decode_princeton_frames(princeton(1), bit_count=65)
    except ValueError:
        pass
    else:
        raise AssertionError("unbounded bit count accepted")
