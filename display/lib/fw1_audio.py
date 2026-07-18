"""Safe speaker tone output and PDM microphone level sampling for FW1."""

from machine import I2S, Pin
import math
import rp2
import struct
import time


@rp2.asm_pio(
    in_shiftdir=rp2.PIO.SHIFT_LEFT,
    sideset_init=rp2.PIO.OUT_HIGH,
)
def _pdm_capture():
    # Matches the four-instruction stock pico_audio_pdm program. Nonblocking
    # pushes are essential: the microphone clock must continue if RX FIFO is
    # temporarily full.
    wrap_target()
    nop().side(0)
    in_(pins, 1).side(0)
    push(iffull, noblock).side(1)
    nop().side(1)
    wrap()


@rp2.asm_pio(
    in_shiftdir=rp2.PIO.SHIFT_LEFT,
    sideset_init=rp2.PIO.OUT_LOW,
)
def _pdm_capture_high_phase():
    wrap_target()
    nop().side(1)
    in_(pins, 1).side(1)
    push(iffull, noblock).side(0)
    nop().side(0)
    wrap()


class PDMLevel:
    DATA_PIN = 29
    CLOCK_PIN = 17
    CLOCK_HZ = 2_048_000

    _NIBBLE_BITS = (0, 1, 1, 2, 1, 2, 2, 3, 1, 2, 2, 3, 2, 3, 3, 4)

    def __init__(
        self,
        state_machine=2,
        clock_hz=CLOCK_HZ,
        sample_on_high=True,
    ):
        self.clock = Pin(self.CLOCK_PIN, Pin.OUT, value=0)
        self.data = Pin(self.DATA_PIN, Pin.IN, pull=None)
        self.state_machine = state_machine
        self.sm = rp2.StateMachine(
            state_machine,
            _pdm_capture_high_phase if sample_on_high else _pdm_capture,
            freq=clock_hz * 4,
            in_base=self.data,
            sideset_base=self.clock,
        )
        self.sm.active(1)
        self.last_level = 0

    @classmethod
    def _popcount32(cls, value):
        table = cls._NIBBLE_BITS
        return (
            table[value & 0xF]
            + table[(value >> 4) & 0xF]
            + table[(value >> 8) & 0xF]
            + table[(value >> 12) & 0xF]
            + table[(value >> 16) & 0xF]
            + table[(value >> 20) & 0xF]
            + table[(value >> 24) & 0xF]
            + table[(value >> 28) & 0xF]
        )

    def read_level(self, max_words=64):
        words = min(self.sm.rx_fifo(), max_words)
        if words <= 0:
            return self.last_level
        deviation = 0
        for _ in range(words):
            deviation += abs(self._popcount32(self.sm.get()) - 16)
        # A simple activity meter, not yet a PCM decimator. Clamp transient
        # saturation while preserving useful low-level motion.
        self.last_level = min(100, (deviation * 7) // words)
        return self.last_level

    def capture_words_dma(self, word_count, timeout_ms=2_000):
        word_count = int(word_count)
        if word_count <= 0:
            return bytearray()

        sm_index = self.state_machine & 3
        pio_index = self.state_machine >> 2
        if pio_index > 1:
            raise ValueError("invalid PIO state machine")
        pio_base = 0x50200000 + pio_index * 0x00100000
        rx_fifo = pio_base + 0x20 + sm_index * 4
        dreq = 4 + sm_index + pio_index * 8

        while self.sm.rx_fifo():
            self.sm.get()
        data = bytearray(word_count * 4)
        dma = rp2.DMA()
        try:
            control = dma.pack_ctrl(
                inc_read=False,
                inc_write=True,
                treq_sel=dreq,
                size=2,
            )
            dma.config(
                read=rx_fifo,
                write=data,
                count=word_count,
                ctrl=control,
                trigger=True,
            )
            deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
            while dma.active():
                if time.ticks_diff(time.ticks_ms(), deadline) >= 0:
                    dma.active(False)
                    raise OSError("PDM DMA timeout")
        finally:
            dma.close()
        return data

    def capture_pcm(self, sample_count=256, gain=512):
        sample_count = int(sample_count)
        words = self.capture_words_dma(sample_count * 8)
        densities = [0] * sample_count
        offset = 0
        for sample in range(sample_count):
            ones = 0
            for _ in range(8):
                value = struct.unpack_from("<I", words, offset)[0]
                offset += 4
                ones += self._popcount32(value)
            densities[sample] = ones

        dc = sum(densities) // max(1, sample_count)
        pcm = [0] * sample_count
        for index, density in enumerate(densities):
            value = (density - dc) * gain
            pcm[index] = max(-32768, min(32767, value))
        return pcm

    def deinit(self):
        self.sm.active(0)
        self.clock.init(Pin.OUT, value=0)


class Speaker:
    DATA_PIN = 4
    BCLK_PIN = 5
    LRCLK_PIN = 6

    def __init__(self, sample_rate=8_000):
        self.sample_rate = sample_rate
        self.i2s = I2S(
            0,
            sck=Pin(self.BCLK_PIN),
            ws=Pin(self.LRCLK_PIN),
            sd=Pin(self.DATA_PIN),
            mode=I2S.TX,
            bits=16,
            format=I2S.STEREO,
            rate=sample_rate,
            ibuf=4096,
        )

    def tone(self, frequency=880, duration_ms=120, volume=0.04):
        frequency = max(50, min(8_000, int(frequency)))
        duration_ms = max(10, min(2_000, int(duration_ms)))
        volume = max(0.0, min(0.10, float(volume)))
        amplitude = int(32767 * volume)
        total_frames = self.sample_rate * duration_ms // 1000
        phase = 0.0
        phase_step = 2.0 * math.pi * frequency / self.sample_rate
        frames_per_buffer = 128
        buffer = bytearray(frames_per_buffer * 4)

        written_frames = 0
        while written_frames < total_frames:
            frame_count = min(frames_per_buffer, total_frames - written_frames)
            for index in range(frame_count):
                sample = int(math.sin(phase) * amplitude)
                phase += phase_step
                if phase >= 2.0 * math.pi:
                    phase -= 2.0 * math.pi
                struct.pack_into("<hh", buffer, index * 4, sample, sample)
            self.i2s.write(memoryview(buffer)[: frame_count * 4])
            written_frames += frame_count

    def deinit(self):
        self.i2s.deinit()
