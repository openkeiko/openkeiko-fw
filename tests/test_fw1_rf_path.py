import importlib.util
from pathlib import Path
import sys
import types


machine = types.ModuleType("machine")
machine.I2C = object
machine.Pin = object
sys.modules["machine"] = machine

ROOT = Path(__file__).parents[1]
DRIVER = ROOT / "display/lib/fw1_i2c.py"
spec = importlib.util.spec_from_file_location("fw1_i2c", DRIVER)
i2c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(i2c)


class FakeBus:
    def __init__(
        self,
        output=0xFF,
        configuration=0xFF,
        port0_output=0xFF,
        port0_configuration=0xFF,
    ):
        self.registers = {
            0x02: port0_output,
            0x03: output,
            0x06: port0_configuration,
            0x07: configuration,
        }
        self.writes = []

    def readfrom_mem(self, address, register, length):
        assert address == 0x21
        assert length == 1
        return bytes((self.registers[register],))

    def writeto_mem(self, address, register, data):
        assert address == 0x21
        self.writes.append((register, bytes(data)))
        self.registers[register] = data[0]


class OneShotReadFailureBus(FakeBus):
    def __init__(self, *args, fail_read_number, **kwargs):
        super().__init__(*args, **kwargs)
        self.read_number = 0
        self.fail_read_number = fail_read_number

    def readfrom_mem(self, address, register, length):
        self.read_number += 1
        if self.read_number == self.fail_read_number:
            raise OSError("injected read failure")
        return super().readfrom_mem(address, register, length)


def controller(bus):
    value = i2c.FW1I2C.__new__(i2c.FW1I2C)
    value.bus = bus
    return value


def test_frequency_to_filter_band_boundaries():
    select = i2c.FW1I2C.radio_band_for_frequency
    assert select(300_000_000) == 1
    assert select(348_000_000) == 1
    assert select(387_000_000) == 2
    assert select(464_000_000) == 2
    assert select(779_000_000) == 3
    assert select(928_000_000) == 3
    for invalid in (299_999_999, 349_000_000, 500_000_000, 928_000_001):
        try:
            select(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid frequency accepted")


def test_recovered_pca_band_bit_encoding():
    encode = i2c.FW1I2C._radio_band_bits
    assert encode(1, 1) == 0x06
    assert encode(2, 2) == 0x18
    assert encode(3, 3) == 0x1E
    assert encode(1, 2) == 0x12


def test_selector_changes_only_four_recovered_outputs():
    bus = FakeBus()
    result = controller(bus).set_radio_bands(2, 2)
    assert result["output"] == 0xF9
    assert result["configuration"] == 0xE1
    assert bus.writes == [(0x03, b"\xF9"), (0x07, b"\xE1")]
    assert (result["output"] & ~0x1E) == (0xFF & ~0x1E)


def test_disable_returns_filter_selects_to_inputs():
    bus = FakeBus(output=0xF9, configuration=0xE1)
    assert controller(bus).disable_radio_bands() == 0xFF
    assert bus.writes == [(0x07, b"\xFF")]


def test_header_output_test_changes_only_recovered_output_paths():
    bus = FakeBus(port0_output=0x25, port0_configuration=0xFF)
    state = controller(bus).enable_header_output_test_paths()
    assert state == {"mask": 0xDA, "output": 0x25, "configuration": 0xFF}
    assert bus.registers[0x02] == 0xFF
    assert bus.registers[0x06] == 0x25
    assert bus.writes == [(0x02, b"\xFF"), (0x06, b"\x25")]


def test_header_output_enable_rolls_back_after_verification_failure():
    bus = OneShotReadFailureBus(
        port0_output=0x25,
        port0_configuration=0xFF,
        fail_read_number=3,
    )
    try:
        controller(bus).enable_header_output_test_paths()
    except OSError:
        pass
    else:
        raise AssertionError("injected verification failure was ignored")
    assert bus.registers[0x02] == 0x25
    assert bus.registers[0x06] == 0xFF


def test_header_output_restore_rejects_incomplete_state_before_writing():
    bus = FakeBus(port0_output=0xFF, port0_configuration=0x25)
    try:
        controller(bus).restore_header_output_test_paths({"mask": 0xDA})
    except ValueError:
        pass
    else:
        raise AssertionError("incomplete state was accepted")
    assert bus.writes == []


def test_header_output_test_restores_direction_before_latches():
    bus = FakeBus(port0_output=0xFF, port0_configuration=0x25)
    state = {"mask": 0xDA, "output": 0x25, "configuration": 0xFF}
    result = controller(bus).restore_header_output_test_paths(state)
    assert result == {"output": 0x25, "configuration": 0xFF}
    assert bus.writes == [
        (0x06, b"\xFF"),
        (0x02, b"\x25"),
        (0x06, b"\xFF"),
    ]
