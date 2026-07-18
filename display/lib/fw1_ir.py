"""Infrared receive activity/NEC decoding and guarded NEC transmit for FW1."""

from machine import Pin, mem32
import rp2
import time


def decode_nec_timings(timings):
    """Decode demodulated mark/space timings using distortion-resistant cells."""
    if len(timings) < 66:
        return None
    if not 8_000 <= timings[0] <= 10_000:
        return None
    if not 3_500 <= timings[1] <= 5_500:
        return None

    value = 0
    for bit in range(32):
        mark = timings[2 + bit * 2]
        space = timings[3 + bit * 2]
        total = mark + space
        if not 100 <= mark <= 1_200 or not 700 <= total <= 3_000:
            return None
        if total > 1_650:
            value |= 1 << bit

    address = value & 0xFF
    address_upper = (value >> 8) & 0xFF
    command = (value >> 16) & 0xFF
    command_upper = (value >> 24) & 0xFF
    if address ^ address_upper == 0xFF and command ^ command_upper == 0xFF:
        return "NEC", address, command
    return "NECext", address | address_upper << 8, command | command_upper << 8


@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW)
def _ir_envelope():
    # Every FIFO pair is mark cycles followed by space cycles. Both loops take
    # exactly 32 state-machine clocks per carrier period, so Python scheduling
    # cannot stretch individual NEC timings.
    pull(block)
    mov(x, osr)
    label("mark")
    set(pins, 1) [10]
    set(pins, 0) [19]
    jmp(x_dec, "mark")
    pull(block)
    mov(x, osr)
    label("space")
    nop() [30]
    jmp(x_dec, "space")


class IRReceiver:
    PIN = 16
    BUFFER_SIZE = 256
    CAPTURE_LIMIT = 512
    FRAME_GAP_US = 30_000

    def __init__(self, pin=PIN):
        self.pin = Pin(pin, Pin.IN)
        self._durations = [0] * self.BUFFER_SIZE
        self._levels = [0] * self.BUFFER_SIZE
        self._write = 0
        self._read = 0
        self._last_edge = time.ticks_us()
        self.activity = 0
        self.overruns = 0
        self._state = 0
        self._value = 0
        self._bits = 0
        self._mark_duration = 0
        self.last_protocol = None
        self.last_failure = None
        self.decode_failures = 0
        self.repeats = 0
        self._capture = []
        self.last_raw = None
        self.captures = 0
        self.enabled = False
        self.resume()

    def _edge(self, pin):
        now = time.ticks_us()
        duration = time.ticks_diff(now, self._last_edge)
        self._last_edge = now
        next_write = (self._write + 1) % self.BUFFER_SIZE
        if next_write == self._read:
            self.overruns += 1
            return
        # The transition ended the opposite level from the pin's new state.
        self._durations[self._write] = duration
        self._levels[self._write] = 1 - pin.value()
        self._write = next_write
        self.activity = (self.activity + 1) & 0xFFFF

    @staticmethod
    def _within(value, minimum, maximum):
        return minimum <= value <= maximum

    def _reset_decoder(self):
        self._state = 0
        self._value = 0
        self._bits = 0
        self._mark_duration = 0

    def _decode_failed(self, reason):
        self.last_failure = reason
        self.decode_failures = (self.decode_failures + 1) & 0xFFFF
        self._reset_decoder()

    def _consume(self, level, duration):
        # Demodulating receivers idle high. NEC begins with 9 ms low and
        # 4.5 ms high, followed by 32 low-pulse/high-space bit cells.
        if self._state == 0:
            if level == 0 and self._within(duration, 8_000, 10_000):
                self._state = 1
            return None
        if self._state == 1:
            if level == 1 and self._within(duration, 3_500, 5_500):
                self._state = 2
            elif level == 1 and self._within(duration, 1_700, 2_800):
                self._state = 4
            else:
                self._decode_failed("LEADER SPACE")
            return None
        if self._state == 4:
            if level == 0 and self._within(duration, 100, 1_200):
                self.repeats = (self.repeats + 1) & 0xFFFF
                self.last_failure = "NEC REPEAT"
                self._reset_decoder()
            else:
                self._decode_failed("REPEAT MARK")
            return None
        if self._state == 2:
            if level == 0 and self._within(duration, 100, 1_200):
                self._mark_duration = duration
                self._state = 3
            else:
                self._decode_failed("BIT MARK")
            return None

        if level != 1:
            self._decode_failed("BIT LEVEL")
            return None
        cell_duration = self._mark_duration + duration
        if not self._within(cell_duration, 700, 3_000):
            self._decode_failed("BIT CELL")
            return None
        bit = 1 if cell_duration > 1_650 else 0

        self._value |= bit << self._bits
        self._bits += 1
        if self._bits < 32:
            self._state = 2
            return None

        value = self._value
        self._reset_decoder()
        address = value & 0xFF
        address_inverse = (value >> 8) & 0xFF
        command = (value >> 16) & 0xFF
        command_inverse = (value >> 24) & 0xFF
        if (
            (address ^ address_inverse) == 0xFF
            and (command ^ command_inverse) == 0xFF
        ):
            self.last_protocol = "NEC"
            self.last_failure = None
            return address, command

        # Flipper's NECext interpretation preserves all four received bytes;
        # neither upper byte is required to be an inverse.
        self.last_protocol = "NECext"
        self.last_failure = None
        return address | (address_inverse << 8), command | (command_inverse << 8)

    def poll(self):
        decoded = []
        while self._read != self._write:
            index = self._read
            self._read = (index + 1) % self.BUFFER_SIZE
            level = self._levels[index]
            duration = self._durations[index]

            if not self._capture:
                if level == 0 and 100 <= duration <= 20_000:
                    self._capture.append(duration)
                    self.last_failure = None
            elif len(self._capture) < self.CAPTURE_LIMIT:
                self._capture.append(duration)

            result = self._consume(level, duration)
            if result is not None:
                decoded.append(result)

        if (
            self._capture
            and self.pin.value()
            and time.ticks_diff(time.ticks_us(), self._last_edge) >= self.FRAME_GAP_US
        ):
            self.last_raw = tuple(self._capture)
            self._capture = []
            self.captures = (self.captures + 1) & 0xFFFF
        return decoded

    def take_raw(self):
        capture = self.last_raw
        self.last_raw = None
        return capture

    def pause(self):
        if self.enabled:
            self.pin.irq(handler=None)
            self.enabled = False

    def resume(self):
        self._write = self._read = 0
        self._capture = []
        self.last_raw = None
        self._last_edge = time.ticks_us()
        self._reset_decoder()
        self.pin.irq(
            trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING,
            handler=self._edge,
        )
        self.enabled = True

    def deinit(self):
        self.pause()


class IRTransmitter:
    PIN = 9
    CARRIER_HZ = 38_000
    PADS_BANK0_GPIO0 = 0x4001C004
    DRIVE_MASK = 0x30
    DRIVE_12MA = 0x30
    MAX_TX_TIMINGS = 512
    MAX_TX_DURATION_US = 2_000_000

    def __init__(self, pin=PIN, carrier_hz=CARRIER_HZ, state_machine=7):
        self.pin_number = pin
        self.pin = Pin(pin, Pin.OUT, value=0)
        pad_register = self.PADS_BANK0_GPIO0 + pin * 4
        mem32[pad_register] = (
            mem32[pad_register] & ~self.DRIVE_MASK
        ) | self.DRIVE_12MA
        self.state_machine = state_machine
        self.sm = None
        self.carrier_hz = 0
        self.duty_cycle = 0.0
        self.set_carrier(carrier_hz, 0.33)

    def set_carrier(self, carrier_hz, duty_cycle=0.33):
        carrier_hz = int(carrier_hz)
        duty_cycle = float(duty_cycle)
        if not 20_000 <= carrier_hz <= 60_000:
            raise ValueError("IR carrier must be 20-60 kHz")
        if not 0.1 <= duty_cycle <= 0.6:
            raise ValueError("IR duty cycle must be 0.1-0.6")
        if carrier_hz == self.carrier_hz and duty_cycle == self.duty_cycle:
            return
        if self.sm is not None:
            self.sm.active(0)
        # The board's validated Flipper/NEC path uses an 11/32 (34.4%) mark
        # duty, close to the conventional one-third carrier. PIO1/SM7 avoids
        # the PIO0 program-space contention found during early testing and also
        # avoids GPIO9's PWM alias with TFT backlight GPIO25.
        self.sm = rp2.StateMachine(
            self.state_machine,
            _ir_envelope,
            freq=carrier_hz * 32,
            set_base=self.pin,
        )
        self.sm.active(1)
        self.carrier_hz = carrier_hz
        self.duty_cycle = duty_cycle

    def _send_envelope(self, timings):
        # Raw files commonly end with a mark. Restarting guarantees every new
        # transmission begins at the program's mark half rather than inheriting
        # a prior wait for the matching space.
        self.sm.active(0)
        self.sm.restart()
        self.pin.value(0)
        self.sm.active(1)
        total_us = 0
        for duration in timings:
            duration = int(duration)
            cycles = max(
                1,
                (duration * self.carrier_hz + 500_000) // 1_000_000,
            )
            self.sm.put(cycles - 1)
            total_us += duration
        # put() blocks as the four-word FIFO fills, leaving at most a few queued
        # segments. This conservative wait affects return latency only; the PIO
        # waveform itself has no Python-timed gaps.
        time.sleep_us(total_us + 1_000)

    def send_nec(self, address, command):
        self.set_carrier(38_000, 0.33)
        if not 0 <= command <= 0xFF:
            raise ValueError("NEC command must fit in one byte")
        if 0 <= address <= 0xFF:
            address_bytes = (address, address ^ 0xFF)
        elif 0 <= address <= 0xFFFF:
            address_bytes = (address & 0xFF, address >> 8)
        else:
            raise ValueError("NEC address must fit in two bytes")

        data = address_bytes + (command, command ^ 0xFF)
        timings = [9_000, 4_500]
        for value in data:
            for bit in range(8):
                timings.append(562)
                timings.append(1_687 if value & (1 << bit) else 562)
        timings.append(562)
        self._send_envelope(timings)

    def send_nec_ext(self, address, command):
        if not 0 <= address <= 0xFFFF or not 0 <= command <= 0xFFFF:
            raise ValueError("NECext address and command must fit in two bytes")
        self.set_carrier(38_000, 0.33)
        data = (address & 0xFF, address >> 8, command & 0xFF, command >> 8)
        timings = [9_000, 4_500]
        for value in data:
            for bit in range(8):
                timings.append(562)
                timings.append(1_687 if value & (1 << bit) else 562)
        timings.append(562)
        self._send_envelope(timings)

    def send_raw(self, timings, carrier_hz=38_000, duty_cycle=0.33):
        if not timings or any(int(duration) <= 0 for duration in timings):
            raise ValueError("IR raw timings must be positive")
        if len(timings) > self.MAX_TX_TIMINGS:
            raise ValueError("IR raw signal has too many timings")
        if sum(int(duration) for duration in timings) > self.MAX_TX_DURATION_US:
            raise ValueError("IR raw signal exceeds two seconds")
        self.set_carrier(carrier_hz, duty_cycle)
        self._send_envelope(timings)

    def deinit(self):
        self.sm.active(0)
        self.pin.init(Pin.OUT, value=0)
