"""Flipper Zero infrared and Sub-GHz file parsing for MicroPython."""


IR_FILETYPE = "IR signals file"
IR_LIBRARY_FILETYPE = "IR library file"
SUB_KEY_FILETYPE = "Flipper SubGhz Key File"
SUB_RAW_FILETYPE = "Flipper SubGhz RAW File"


def _split_field(line):
    if ":" not in line:
        raise ValueError("invalid Flipper field: {}".format(line))
    key, value = line.split(":", 1)
    return key.strip(), value.strip()


def _integer(value, field):
    try:
        return int(value, 0)
    except (TypeError, ValueError):
        raise ValueError("invalid {}".format(field))


def parse_hex_bytes(value):
    try:
        return bytes(int(item, 16) for item in value.split())
    except (TypeError, ValueError):
        raise ValueError("invalid hex byte array")


def format_hex_bytes(value):
    return " ".join("{:02X}".format(item) for item in value)


def little_endian_hex(value, width=4):
    return bytes((value >> (8 * index)) & 0xFF for index in range(width))


def hex_little_endian(value):
    result = 0
    for index, item in enumerate(parse_hex_bytes(value)):
        result |= item << (8 * index)
    return result


def _validate_custom_preset(data):
    if len(data) < 10 or len(data) & 1:
        raise ValueError("invalid custom CC1101 preset")
    terminator = -1
    for index in range(0, len(data) - 1, 2):
        if data[index] == 0 and data[index + 1] == 0:
            terminator = index
            break
    if terminator < 0 or len(data) - terminator - 2 != 8:
        raise ValueError("custom CC1101 preset needs terminator and PATABLE")
    return data


def _parse_lines(source):
    if hasattr(source, "read"):
        source = source.read()
    if isinstance(source, bytes):
        source = source.decode()
    return source.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def parse_ir(source):
    header = {}
    signals = []
    current = None

    for raw_line in _parse_lines(source):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            if current and current.get("name"):
                signals.append(current)
                current = None
            continue

        key, value = _split_field(line)
        if key in ("Filetype", "Version") and current is None and not signals:
            header[key] = value
            continue
        if key == "name":
            if current and current.get("name"):
                signals.append(current)
            current = {"name": value}
            continue
        if current is None:
            raise ValueError("IR signal field before name")
        current[key] = value

    if current and current.get("name"):
        signals.append(current)

    if header.get("Filetype") not in (IR_FILETYPE, IR_LIBRARY_FILETYPE):
        raise ValueError("unsupported IR Filetype")
    if _integer(header.get("Version", "-1"), "IR Version") != 1:
        raise ValueError("unsupported IR Version")

    parsed = []
    for fields in signals:
        signal_type = fields.get("type")
        signal = {"name": fields["name"], "type": signal_type}
        if signal_type == "parsed":
            for required in ("protocol", "address", "command"):
                if required not in fields:
                    raise ValueError("missing IR {}".format(required))
            address = parse_hex_bytes(fields["address"])
            command = parse_hex_bytes(fields["command"])
            if len(address) != 4 or len(command) != 4:
                raise ValueError("IR address and command must contain four bytes")
            signal.update(
                protocol=fields["protocol"],
                address=hex_little_endian(fields["address"]),
                command=hex_little_endian(fields["command"]),
            )
        elif signal_type == "raw":
            for required in ("frequency", "duty_cycle", "data"):
                if required not in fields:
                    raise ValueError("missing IR {}".format(required))
            data = [_integer(item, "IR data") for item in fields["data"].split()]
            if not data or any(item <= 0 for item in data):
                raise ValueError("IR data must contain positive timings")
            frequency = _integer(fields["frequency"], "IR frequency")
            duty_cycle = float(fields["duty_cycle"])
            if frequency <= 0 or not 0.0 < duty_cycle <= 1.0:
                raise ValueError("invalid IR carrier")
            signal.update(
                frequency=frequency,
                duty_cycle=duty_cycle,
                data=data,
            )
        else:
            raise ValueError("unsupported IR signal type")
        parsed.append(signal)

    return {"filetype": header["Filetype"], "version": 1, "signals": parsed}


def format_ir(document):
    filetype = document.get("filetype", IR_FILETYPE)
    if filetype not in (IR_FILETYPE, IR_LIBRARY_FILETYPE):
        raise ValueError("unsupported IR Filetype")
    lines = ["Filetype: {}".format(filetype), "Version: 1"]
    for signal in document.get("signals", ()):
        lines.extend(("#", "name: {}".format(signal["name"])))
        signal_type = signal["type"]
        lines.append("type: {}".format(signal_type))
        if signal_type == "parsed":
            lines.extend(
                (
                    "protocol: {}".format(signal["protocol"]),
                    "address: {}".format(
                        format_hex_bytes(little_endian_hex(signal["address"]))
                    ),
                    "command: {}".format(
                        format_hex_bytes(little_endian_hex(signal["command"]))
                    ),
                )
            )
        elif signal_type == "raw":
            lines.extend(
                (
                    "frequency: {}".format(int(signal["frequency"])),
                    "duty_cycle: {:.6f}".format(float(signal["duty_cycle"])),
                    "data: {}".format(" ".join(str(int(item)) for item in signal["data"])),
                )
            )
        else:
            raise ValueError("unsupported IR signal type")
    return "\n".join(lines) + "\n"


def load_ir(path):
    with open(path, "r") as source:
        return parse_ir(source)


def save_ir(path, document):
    with open(path, "w") as output:
        output.write(format_ir(document))


def parse_sub(source):
    fields = {}
    repeated = {"RAW_Data": [], "Bit_RAW": [], "Data_RAW": []}
    for raw_line in _parse_lines(source):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = _split_field(line)
        if key in repeated:
            repeated[key].append(value)
        else:
            fields[key] = value

    filetype = fields.get("Filetype")
    if filetype not in (SUB_KEY_FILETYPE, SUB_RAW_FILETYPE):
        raise ValueError("unsupported SubGhz Filetype")
    if _integer(fields.get("Version", "-1"), "SubGhz Version") != 1:
        raise ValueError("unsupported SubGhz Version")
    for required in ("Frequency", "Preset", "Protocol"):
        if required not in fields:
            raise ValueError("missing SubGhz {}".format(required))

    frequency = _integer(fields["Frequency"], "SubGhz frequency")
    if frequency <= 0:
        raise ValueError("invalid SubGhz frequency")
    result = {
        "filetype": filetype,
        "version": 1,
        "frequency": frequency,
        "preset": fields["Preset"],
        "protocol": fields["Protocol"],
    }
    if fields["Preset"] == "FuriHalSubGhzPresetCustom":
        if fields.get("Custom_preset_module") != "CC1101":
            raise ValueError("unsupported custom SubGhz module")
        result["custom_preset_module"] = "CC1101"
        result["custom_preset_data"] = _validate_custom_preset(
            parse_hex_bytes(fields.get("Custom_preset_data", ""))
        )

    protocol = fields["Protocol"]
    if protocol == "RAW":
        raw_data = []
        for line in repeated["RAW_Data"]:
            raw_data.extend(_integer(item, "RAW_Data") for item in line.split())
        if not raw_data or any(item == 0 for item in raw_data):
            raise ValueError("RAW_Data must contain nonzero timings")
        result["raw_data"] = raw_data
    elif protocol == "BinRAW":
        result["bit"] = _integer(fields.get("Bit", "0"), "Bit")
        result["te"] = _integer(fields.get("TE", "0"), "TE")
        result["bit_raw"] = [_integer(value, "Bit_RAW") for value in repeated["Bit_RAW"]]
        result["data_raw"] = [parse_hex_bytes(value) for value in repeated["Data_RAW"]]
        if len(result["bit_raw"]) != len(result["data_raw"]):
            raise ValueError("BinRAW blocks are not paired")
    else:
        if "Bit" in fields:
            result["bit"] = _integer(fields["Bit"], "Bit")
        if "Key" in fields:
            result["key"] = parse_hex_bytes(fields["Key"])
            if len(result["key"]) != 8:
                raise ValueError("SubGhz Key must contain eight bytes")
        if "TE" in fields:
            result["te"] = _integer(fields["TE"], "TE")
        result["fields"] = {
            key: value
            for key, value in fields.items()
            if key not in (
                "Filetype",
                "Version",
                "Frequency",
                "Preset",
                "Protocol",
                "Bit",
                "Key",
                "TE",
                "Custom_preset_module",
                "Custom_preset_data",
            )
        }
    return result


def _append_timing_lines(lines, name, timings, limit=512):
    for offset in range(0, len(timings), limit):
        chunk = timings[offset : offset + limit]
        lines.append("{}: {}".format(name, " ".join(str(int(item)) for item in chunk)))


def format_sub(document):
    filetype = document["filetype"]
    if filetype not in (SUB_KEY_FILETYPE, SUB_RAW_FILETYPE):
        raise ValueError("unsupported SubGhz Filetype")
    lines = [
        "Filetype: {}".format(filetype),
        "Version: 1",
        "Frequency: {}".format(int(document["frequency"])),
        "Preset: {}".format(document["preset"]),
    ]
    if document["preset"] == "FuriHalSubGhzPresetCustom":
        lines.extend(
            (
                "Custom_preset_module: {}".format(document["custom_preset_module"]),
                "Custom_preset_data: {}".format(
                    format_hex_bytes(document["custom_preset_data"])
                ),
            )
        )
    protocol = document["protocol"]
    lines.append("Protocol: {}".format(protocol))
    if protocol == "RAW":
        _append_timing_lines(lines, "RAW_Data", document["raw_data"])
    elif protocol == "BinRAW":
        lines.extend(("Bit: {}".format(document["bit"]), "TE: {}".format(document["te"])))
        for bit_raw, data_raw in zip(document["bit_raw"], document["data_raw"]):
            lines.append("Bit_RAW: {}".format(bit_raw))
            lines.append("Data_RAW: {}".format(format_hex_bytes(data_raw)))
    else:
        if "bit" in document:
            lines.append("Bit: {}".format(document["bit"]))
        if "key" in document:
            lines.append("Key: {}".format(format_hex_bytes(document["key"])))
        if "te" in document:
            lines.append("TE: {}".format(document["te"]))
        for key, value in document.get("fields", {}).items():
            lines.append("{}: {}".format(key, value))
    return "\n".join(lines) + "\n"


def load_sub(path):
    with open(path, "r") as source:
        return parse_sub(source)


def save_sub(path, document):
    with open(path, "w") as output:
        output.write(format_sub(document))
