import subprocess
import sys
from pathlib import Path
import zipfile


ROOT = Path(__file__).parents[1]
ANALYZER = ROOT / "scripts/analyze_fw1_output_capture.py"
CHANNELS = (2, 3, 5, 4, 6, 7)


def write_capture(path, channels=CHANNELS, sample_rate="1 MHz"):
    samples = bytearray(b"\xff" * 160_000)
    for index, channel in enumerate(channels):
        start = 10_000 + index * 20_000
        for sample in range(start, start + 10_000):
            samples[sample] &= ~(1 << channel)
    metadata = """[global]
sigrok version=0.5.2

[device 1]
capturefile=logic-1
total probes=8
samplerate={}
total analog=0
probe1=D0
probe2=D1
probe3=D2
probe4=D3
probe5=D4
probe6=D5
probe7=D6
probe8=D7
unitsize=1
""".format(sample_rate)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("version", "2")
        archive.writestr("metadata", metadata)
        archive.writestr("logic-1-1", samples)


def run_analyzer(path):
    return subprocess.run(
        [sys.executable, str(ANALYZER), str(path)],
        capture_output=True,
        text=True,
    )


def test_analyzer_accepts_exact_channel_order(tmp_path):
    capture = tmp_path / "correct.sr"
    write_capture(capture)
    result = run_analyzer(capture)
    assert result.returncode == 0, result.stderr
    assert "validated six pulses" in result.stdout


def test_analyzer_rejects_chronologically_correct_wrong_channels(tmp_path):
    capture = tmp_path / "wrong.sr"
    write_capture(capture, channels=(0, 1, 2, 3, 4, 5))
    result = run_analyzer(capture)
    assert result.returncode != 0
    assert "expected on D2, found D0" in result.stderr


def test_analyzer_rejects_metadata_sample_rate_mismatch(tmp_path):
    capture = tmp_path / "wrong-rate.sr"
    write_capture(capture, sample_rate="2 MHz")
    result = run_analyzer(capture)
    assert result.returncode != 0
    assert "does not match" in result.stderr
