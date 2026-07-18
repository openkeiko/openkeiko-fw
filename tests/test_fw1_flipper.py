import importlib.util
from pathlib import Path


MODULE_PATH = (
    Path(__file__).parents[1]
    / "display"
    / "lib"
    / "fw1_flipper.py"
)
spec = importlib.util.spec_from_file_location("fw1_flipper", MODULE_PATH)
flipper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flipper)


IR_SAMPLE = """Filetype: IR signals file
Version: 1
#
name: Power
 type: parsed
protocol: NECext
address: EE 87 00 00
command: 5D A0 00 00
#
name: Raw
 type: raw
frequency: 38000
duty_cycle: 0.330000
data: 9000 4500 562 562 562 1687
"""

SUB_KEY_SAMPLE = """Filetype: Flipper SubGhz Key File
Version: 1
Frequency: 433920000
Preset: FuriHalSubGhzPresetOok650Async
Protocol: Princeton
Bit: 24
Key: 00 00 00 00 00 95 D5 D4
TE: 400
"""

SUB_RAW_SAMPLE = """Filetype: Flipper SubGhz RAW File
Version: 1
Frequency: 433920000
Preset: FuriHalSubGhzPresetOok650Async
Protocol: RAW
RAW_Data: 29262 361 -68 2635 -66
RAW_Data: 205 -412 159 -412
"""

SUB_CUSTOM_SAMPLE = """Filetype: Flipper SubGhz Key File
Version: 1
Frequency: 433920000
Preset: FuriHalSubGhzPresetCustom
Custom_preset_module: CC1101
Custom_preset_data: 02 0D 03 07 00 00 C0 00 00 00 00 00 00 00
Protocol: Princeton
Bit: 24
Key: 00 00 00 00 00 95 D5 D4
TE: 400
"""

SUB_BINRAW_SAMPLE = """Filetype: Flipper SubGhz Key File
Version: 1
Frequency: 433920000
Preset: FuriHalSubGhzPresetOok650Async
Protocol: BinRAW
Bit: 16
TE: 597
Bit_RAW: 8
Data_RAW: A5
Bit_RAW: 8
Data_RAW: 5A
"""


def test_ir_parse_and_round_trip():
    document = flipper.parse_ir(IR_SAMPLE)
    assert len(document["signals"]) == 2
    assert document["signals"][0]["address"] == 0x87EE
    assert document["signals"][0]["command"] == 0xA05D
    assert document["signals"][1]["data"][-1] == 1687
    assert flipper.parse_ir(flipper.format_ir(document)) == document


def test_ir_library_filetype():
    source = IR_SAMPLE.replace("IR signals file", "IR library file")
    assert flipper.parse_ir(source)["filetype"] == "IR library file"


def test_subghz_key_parse_and_round_trip():
    document = flipper.parse_sub(SUB_KEY_SAMPLE)
    assert document["frequency"] == 433_920_000
    assert document["protocol"] == "Princeton"
    assert document["key"] == bytes.fromhex("00 00 00 00 00 95 D5 D4")
    assert document["te"] == 400
    assert flipper.parse_sub(flipper.format_sub(document)) == document


def test_subghz_custom_preset_round_trip():
    document = flipper.parse_sub(SUB_CUSTOM_SAMPLE)
    assert document["custom_preset_module"] == "CC1101"
    assert document["custom_preset_data"][-8:] == bytes.fromhex("C0 00 00 00 00 00 00 00")
    assert flipper.parse_sub(flipper.format_sub(document)) == document


def test_subghz_binraw_round_trip():
    document = flipper.parse_sub(SUB_BINRAW_SAMPLE)
    assert document["bit_raw"] == [8, 8]
    assert document["data_raw"] == [b"\xA5", b"\x5A"]
    assert flipper.parse_sub(flipper.format_sub(document)) == document


def test_subghz_raw_multiline_parse_and_round_trip():
    document = flipper.parse_sub(SUB_RAW_SAMPLE)
    assert document["raw_data"] == [29262, 361, -68, 2635, -66, 205, -412, 159, -412]
    assert flipper.parse_sub(flipper.format_sub(document)) == document


def test_rejects_zero_raw_timing():
    invalid = SUB_RAW_SAMPLE.replace("-68", "0", 1)
    try:
        flipper.parse_sub(invalid)
    except ValueError as error:
        assert "nonzero" in str(error)
    else:
        raise AssertionError("zero timing accepted")
