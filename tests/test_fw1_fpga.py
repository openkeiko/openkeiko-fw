import hashlib
import importlib.util
import pathlib
import sys
import types


class FakeMem32(dict):
    def __getitem__(self, address):
        return self.get(address, 0)


class FakePin:
    IN = 0
    OUT = 1
    ALT = 2
    states = {}
    modes = {}

    def __init__(self, pin, mode=None, pull=None, value=None, **kwargs):
        self.pin = pin
        if mode is not None:
            self.init(mode, value=value, **kwargs)

    def init(self, mode, value=None, **kwargs):
        self.modes[self.pin] = mode
        if value is not None:
            self.states[self.pin] = value

    def value(self, value=None):
        if value is None:
            return self.states.get(self.pin, 0)
        self.states[self.pin] = value


class FakeSPI:
    MSB = 0
    instances = []

    def __init__(self, spi_id, **kwargs):
        self.spi_id = spi_id
        self.kwargs = kwargs
        self.writes = []
        self.deinitialized = False
        self.instances.append(self)

    def write(self, data):
        self.writes.append(bytes(data))
        if len(data) >= 64 * 1024:
            FakePin.states[24] = 1

    def deinit(self):
        self.deinitialized = True


fake_time = types.ModuleType("time")
fake_time.now = 0


def sleep_us(value):
    fake_time.now += max(1, value // 1000)


def sleep_ms(value):
    fake_time.now += value


fake_time.sleep_us = sleep_us
fake_time.sleep_ms = sleep_ms
fake_time.ticks_ms = lambda: fake_time.now
fake_time.ticks_add = lambda value, delta: value + delta
fake_time.ticks_diff = lambda a, b: a - b

machine = types.ModuleType("machine")
machine.Pin = FakePin
machine.SPI = FakeSPI
machine.mem32 = FakeMem32()
sys.modules["machine"] = machine
sys.modules["time"] = fake_time

path = pathlib.Path(__file__).parents[1] / "main/lib/fw1_fpga.py"
spec = importlib.util.spec_from_file_location("fw1_fpga", path)
fpga = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fpga)


def reset_fakes():
    FakePin.states.clear()
    FakePin.modes.clear()
    FakeSPI.instances.clear()
    machine.mem32.clear()
    fake_time.now = 0


def image():
    data = bytearray(104_090)
    data[16:20] = fpga.FW1FPGAProgrammer.SYNC_WORD
    return bytes(data)


def test_validates_up5k_sized_image():
    metadata = fpga.FW1FPGAProgrammer.validate(image())
    assert metadata["size"] == 104_090
    assert metadata["sha256"] == hashlib.sha256(image()).hexdigest()


def test_rejects_wrong_size_and_missing_sync():
    for data in (b"short", bytes(104_090)):
        try:
            fpga.FW1FPGAProgrammer.validate(data)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid bitstream accepted")


def test_program_sequence_is_bounded_and_releases_pins():
    reset_fakes()
    programmer = fpga.FW1FPGAProgrammer()
    result = programmer.program(image())
    spi = FakeSPI.instances[-1]
    assert [len(write) for write in spi.writes] == [1, 104_090, 13, 7]
    assert spi.kwargs["baudrate"] == 5_000_000
    assert spi.kwargs["polarity"] == 0
    assert spi.kwargs["phase"] == 0
    assert result["cdone"] is True
    assert FakePin.modes[29] == FakePin.IN
    assert FakePin.modes[13] == FakePin.IN
    assert spi.deinitialized is True


def test_clock_uses_gpout1_with_divide_by_four():
    reset_fakes()
    clock = fpga.FW1FPGAClock()
    assert clock.start(4) == 31_250_000
    assert machine.mem32[clock.CLOCKS_CLK_GPOUT1_DIV] == 4 << 8
    assert clock.running
    gpio_ctrl = clock.IO_BANK0_GPIO_CTRL + clock.PIN * 8
    assert machine.mem32[gpio_ctrl] & 0x1F == clock.GPIO_FUNC_GPCK
    clock.stop()
    assert not clock.running
    assert FakePin.modes[23] == FakePin.IN
