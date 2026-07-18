import importlib.util
from pathlib import Path
import sys
import types


class FakePin:
    IN = 0
    OUT = 1
    PULL_UP = 2
    IRQ_RISING = 1
    IRQ_FALLING = 2


machine = types.ModuleType("machine")
machine.Pin = FakePin
machine.mem32 = {}
sys.modules["machine"] = machine

rp2 = types.ModuleType("rp2")


class FakePIO:
    OUT_LOW = 0
    SHIFT_RIGHT = 0


rp2.PIO = FakePIO
rp2.asm_pio = lambda **kwargs: (lambda function: function)
rp2.StateMachine = object
sys.modules["rp2"] = rp2

ROOT = Path(__file__).parents[1]
MODULE = ROOT / "display/lib/fw1_ir.py"
spec = importlib.util.spec_from_file_location("fw1_ir", MODULE)
ir = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ir)


def receiver():
    value = ir.IRReceiver.__new__(ir.IRReceiver)
    value._state = 0
    value._value = 0
    value._bits = 0
    value.last_protocol = None
    value.last_failure = None
    value.decode_failures = 0
    value.repeats = 0
    return value


def feed_nec(value, data):
    result = value._consume(0, 9000)
    result = value._consume(1, 4500)
    for byte in data:
        for bit in range(8):
            result = value._consume(0, 562)
            result = value._consume(1, 1687 if byte & (1 << bit) else 562)
    return result


def test_decodes_classic_nec():
    value = receiver()
    assert feed_nec(value, (0x10, 0xEF, 0x34, 0xCB)) == (0x10, 0x34)
    assert value.last_protocol == "NEC"
    assert value.last_failure is None


def test_decodes_flipper_necext_without_inverse_bytes():
    value = receiver()
    assert feed_nec(value, (0xEE, 0x87, 0x5D, 0xA0)) == (0x87EE, 0xA05D)
    assert value.last_protocol == "NECext"
    assert value.last_failure is None


def test_decodes_distorted_saved_capture_by_cell_duration():
    timings = (
        9148, 4426, 513, 624, 613, 622, 448, 2039, 145, 852, 298, 669,
        890, 272, 752, 776, 132, 789, 769, 1455, 809, 1330, 813, 191,
        710, 1484, 775, 1443, 776, 1461, 820, 1437, 771, 1470, 821, 1768,
        142, 1571, 529, 626, 600, 1692, 467, 597, 516, 632, 570, 683,
        452, 627, 533, 586, 603, 856, 305, 1744, 453, 585, 505, 1636,
        537, 1719, 472, 1687, 541, 1718, 513,
    )
    assert ir.decode_nec_timings(timings) == ("NEC", 0x04, 0x0B)
    value = receiver()
    result = None
    for index, duration in enumerate(timings):
        result = value._consume(0 if index % 2 == 0 else 1, duration) or result
    assert result == (0x04, 0x0B)
    assert value.last_protocol == "NEC"


def test_reports_failed_bit_cell():
    value = receiver()
    value._consume(0, 9000)
    value._consume(1, 4500)
    value._consume(0, 562)
    assert value._consume(1, 3000) is None
    assert value.last_failure == "BIT CELL"
    assert value.decode_failures == 1


def test_recognizes_repeat_without_inventing_new_code():
    value = receiver()
    value._consume(0, 9000)
    value._consume(1, 2250)
    assert value._consume(0, 562) is None
    assert value.last_failure == "NEC REPEAT"
    assert value.repeats == 1
