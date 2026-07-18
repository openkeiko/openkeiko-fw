#!/usr/bin/env python3
"""Validate the bounded FW1 six-output identification pattern in a sigrok file."""

import argparse
from pathlib import Path
import re
import zipfile


OUTPUTS = (
    ("SPI_CS_Out", 1, 2),
    ("GPIO_27_Out", 3, 3),
    ("UART_Tx_Out", 9, 5),
    ("UART_RTS_Out", 11, 4),
    ("SPI_MOSI_Out", 13, 6),
    ("SPI_Clk_Out", 15, 7),
)


def metadata_sample_rate(metadata):
    match = re.search(r"^samplerate=(\d+)\s*([kM]?)Hz$", metadata, re.MULTILINE)
    if not match:
        raise ValueError("sigrok metadata has no supported samplerate")
    scale = {"": 1, "k": 1_000, "M": 1_000_000}[match.group(2)]
    return int(match.group(1)) * scale


def load_logic(path, expected_sample_rate):
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if "version" not in names or "metadata" not in names:
            raise ValueError("capture is not a sigrok session archive")
        metadata = archive.read("metadata").decode("ascii")
        if "unitsize=1" not in metadata or "total probes=8" not in metadata:
            raise ValueError("capture must contain eight packed digital channels")
        for channel in range(8):
            if "probe{}=D{}".format(channel + 1, channel) not in metadata:
                raise ValueError("unexpected analyzer channel metadata")
        sample_rate = metadata_sample_rate(metadata)
        if sample_rate != expected_sample_rate:
            raise ValueError(
                "capture sample rate {} does not match {}".format(
                    sample_rate, expected_sample_rate
                )
            )

        chunks = [name for name in names if name.startswith("logic-1-")]
        chunks.sort(key=lambda name: int(name.rsplit("-", 1)[1]))
        if not chunks:
            raise ValueError("sigrok capture contains no digital logic chunks")
        return b"".join(archive.read(name) for name in chunks)


def low_runs(samples, channel, minimum_samples):
    runs = []
    start = None
    for index, value in enumerate(samples):
        low = not ((value >> channel) & 1)
        if low and start is None:
            start = index
        elif not low and start is not None:
            if index - start >= minimum_samples:
                runs.append((start, index))
            start = None
    if start is not None and len(samples) - start >= minimum_samples:
        runs.append((start, len(samples)))
    return runs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("capture", type=Path)
    parser.add_argument("--sample-rate", type=int, default=1_000_000)
    parser.add_argument("--minimum-us", type=int, default=1_000)
    parser.add_argument("--pulse-us", type=int, default=10_000)
    parser.add_argument("--spacing-us", type=int, default=20_000)
    parser.add_argument("--tolerance-us", type=int, default=100)
    args = parser.parse_args()

    try:
        samples = load_logic(args.capture, args.sample_rate)
    except (OSError, UnicodeError, ValueError, zipfile.BadZipFile) as error:
        raise SystemExit(str(error))
    minimum = args.minimum_us * args.sample_rate // 1_000_000
    found = []
    for channel in range(8):
        for start, end in low_runs(samples, channel, minimum):
            found.append((start, end, channel))
    found.sort()

    if len(found) != len(OUTPUTS):
        raise SystemExit("expected six substantial low pulses, found {}".format(len(found)))

    tolerance = args.tolerance_us * args.sample_rate // 1_000_000
    expected_width = args.pulse_us * args.sample_rate // 1_000_000
    expected_spacing = args.spacing_us * args.sample_rate // 1_000_000
    starts = []
    for (start, end, channel), (signal, header_pin, expected_channel) in zip(
        found, OUTPUTS
    ):
        if channel != expected_channel:
            raise SystemExit(
                "{} expected on D{}, found D{}".format(
                    signal, expected_channel, channel
                )
            )
        width = end - start
        if abs(width - expected_width) > tolerance:
            raise SystemExit("{} pulse width {} samples is out of range".format(signal, width))
        starts.append(start)
        print(
            "{} header={} analyzer=D{} start={} width_us={:.3f}".format(
                signal,
                header_pin,
                channel,
                start,
                width * 1_000_000 / args.sample_rate,
            )
        )

    for first, second in zip(starts, starts[1:]):
        spacing = second - first
        if abs(spacing - expected_spacing) > tolerance:
            raise SystemExit("pulse start spacing {} samples is out of range".format(spacing))
    print("validated six pulses in {} samples".format(len(samples)))


if __name__ == "__main__":
    main()
