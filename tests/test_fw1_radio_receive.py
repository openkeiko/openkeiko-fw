import importlib.util
from pathlib import Path
import sys
import types


class FakePin:
    IN = 0
    OUT = 1
    IRQ_RISING = 1
    IRQ_FALLING = 2

    def __init__(self, *args, **kwargs):
        pass


class FakeSPI:
    MSB = 0


machine = types.ModuleType("machine")
machine.Pin = FakePin
machine.SPI = FakeSPI
sys.modules["machine"] = machine

rp2 = types.ModuleType("rp2")


class FakePIO:
    SHIFT_LEFT = 0


rp2.PIO = FakePIO
rp2.asm_pio = lambda **kwargs: (lambda function: function)
rp2.StateMachine = object
rp2.DMA = object
sys.modules["rp2"] = rp2

ROOT = Path(__file__).parents[1]
DRIVER = ROOT / "main/lib/fw1_cc1101.py"
spec = importlib.util.spec_from_file_location("fw1_cc1101", DRIVER)
radio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(radio)


def test_rssi_status_conversion():
    convert = radio.CC1101.rssi_dbm
    assert convert(0x00) == -74
    assert convert(0x80) == -138
    assert convert(0xFE) == -75
    assert convert(0x7F) == -11


def test_receive_frequency_allowlist():
    allowed = radio.CC1101.frequency_allowed
    band = radio.CC1101.band_for_frequency
    assert band(315_000_000) == 1
    assert band(433_920_000) == 2
    assert band(915_000_000) == 3
    assert allowed(315_000_000)
    assert allowed(433_920_000)
    assert allowed(915_000_000)
    assert not allowed(100_000_000)
    assert not allowed(500_000_000)


def test_radio_band_status_falls_back_for_missing_radio():
    radios = radio.FW1Radios.__new__(radio.FW1Radios)
    radios.active_frequency_hz = 433_920_000
    radios.radios = (
        types.SimpleNamespace(frequency_hz=0),
        types.SimpleNamespace(frequency_hz=915_000_000),
    )
    assert radios.radio_bands() == (2, 3)


def test_flipper_profiles_are_async_receive_only():
    for preset, profile in radio.FLIPPER_RX_PRESETS.items():
        registers = dict(profile)
        assert registers[0x02] == 0x0D
        assert registers[0x08] == 0x32
        assert all(0 <= address <= 0x2E for address, _ in profile)
        assert 0x3E not in registers


def test_custom_preset_drops_patable():
    data = bytes.fromhex("02 0D 08 32 00 00 C0 00 00 00 00 00 00 00")
    profile = radio.FW1Radios.custom_receive_profile(data)
    assert profile == ((0x02, 0x0D), (0x08, 0x32))


def test_dma_capture_builds_signed_flipper_timings():
    capture = radio.AsyncCapture.__new__(radio.AsyncCapture)
    capture._capture = []
    capture._ready = []
    capture.truncated = False
    capture._finish_run(0, capture.FRAME_GAP_SAMPLES)
    capture._finish_run(1, 100)
    capture._finish_run(0, 50)
    capture._finish_run(1, 200)
    capture._finish_run(0, 60)
    capture._finish_run(1, 100)
    capture._finish_run(0, capture.FRAME_GAP_SAMPLES)
    timings, truncated = capture._ready.pop(0)
    assert timings == [500, -250, 1000, -300, 500]
    assert not truncated


def test_custom_preset_requires_terminator_and_eight_pa_bytes():
    try:
        radio.FW1Radios.custom_receive_profile(bytes.fromhex("02 0D 00 00 C0"))
    except ValueError:
        pass
    else:
        raise AssertionError("malformed custom preset accepted")
