"""Framed main/display UART link for the OpenKeiko FW1 RP2040s."""

from machine import Pin, UART
import struct
import time


FRAME = 0x7E
ESCAPE = 0x7D
ESCAPE_XOR = 0x20
PROTOCOL_VERSION = 1
MAX_PAYLOAD = 512

HELLO = 0x01
HEARTBEAT = 0x02
DISPLAY_STATUS = 0x10
MAIN_STATUS = 0x11
RADIO_PACKET = 0x12
RADIO_CONFIG_RESULT = 0x13
RADIO_RAW_CAPTURE = 0x14
RADIO_SET_BANDS = 0x15
RADIO_BAND_RESULT = 0x16
RADIO_DECODED = 0x17
BUTTON_EVENT = 0x20
IR_RECEIVED = 0x21
PING = 0x30
PONG = 0x31
IR_NEC_COMMAND = 0x40
TONE_COMMAND = 0x41
RADIO_CONFIGURE_RX = 0x42
POWER_PREPARE = 0x50
POWER_READY = 0x51
POWER_RESUME = 0x52

ROLE_MAIN = 1
ROLE_DISPLAY = 2


def crc16_ccitt(data, initial=0xFFFF):
    crc = initial
    for value in data:
        crc ^= value << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def encode_frame(message_type, sequence, payload=b"", flags=0):
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload too large")

    body = bytearray(struct.pack(
        "<BBBBH",
        PROTOCOL_VERSION,
        message_type,
        sequence,
        flags,
        len(payload),
    ))
    body.extend(payload)
    body.extend(struct.pack("<H", crc16_ccitt(body)))

    encoded = bytearray((FRAME,))
    for value in body:
        if value == FRAME or value == ESCAPE:
            encoded.append(ESCAPE)
            encoded.append(value ^ ESCAPE_XOR)
        else:
            encoded.append(value)
    encoded.append(FRAME)
    return encoded


class FrameParser:
    def __init__(self):
        self.buffer = bytearray()
        self.collecting = False
        self.escaped = False
        self.crc_errors = 0
        self.format_errors = 0
        self.frames = 0

    def _decode(self):
        data = self.buffer
        if len(data) < 8:
            self.format_errors += 1
            return None

        version, message_type, sequence, flags, length = struct.unpack(
            "<BBBBH", data[:6]
        )
        if version != PROTOCOL_VERSION or length > MAX_PAYLOAD:
            self.format_errors += 1
            return None
        if len(data) != 6 + length + 2:
            self.format_errors += 1
            return None

        expected_crc = data[-2] | (data[-1] << 8)
        if crc16_ccitt(data[:-2]) != expected_crc:
            self.crc_errors += 1
            return None

        self.frames += 1
        return message_type, sequence, flags, bytes(data[6:-2])

    def feed(self, data):
        packets = []
        for value in data:
            if value == FRAME:
                if self.collecting and self.buffer:
                    packet = self._decode()
                    if packet is not None:
                        packets.append(packet)
                self.buffer = bytearray()
                self.collecting = True
                self.escaped = False
                continue

            if not self.collecting:
                continue
            if self.escaped:
                value ^= ESCAPE_XOR
                self.escaped = False
            elif value == ESCAPE:
                self.escaped = True
                continue

            if len(self.buffer) >= MAX_PAYLOAD + 8:
                self.buffer = bytearray()
                self.collecting = False
                self.escaped = False
                self.format_errors += 1
            else:
                self.buffer.append(value)
        return packets


class FW1Link:
    BAUDRATE = 8_000_000
    LINK_TIMEOUT_MS = 3_000

    def __init__(self, role, baudrate=BAUDRATE):
        if role == ROLE_MAIN:
            tx, rx, cts, rts = 0, 1, 2, 3
        elif role == ROLE_DISPLAY:
            # Both RP2040 UART0 peripherals use their hardware-valid pin roles;
            # the PCB crosses each signal to its peer's complementary pin.
            tx, rx, cts, rts = 0, 1, 2, 3
        else:
            raise ValueError("invalid FW1 link role")

        self.role = role
        self.uart = UART(
            0,
            baudrate=baudrate,
            bits=8,
            parity=None,
            stop=1,
            tx=Pin(tx),
            rx=Pin(rx),
            cts=Pin(cts),
            rts=Pin(rts),
            flow=UART.CTS | UART.RTS,
            timeout=0,
            timeout_char=0,
            rxbuf=4096,
            txbuf=2048,
        )
        self.parser = FrameParser()
        self.sequence = 0
        self.last_rx_ms = None
        self.last_tx_ms = None
        self.tx_errors = 0

    @property
    def connected(self):
        if self.last_rx_ms is None:
            return False
        return time.ticks_diff(time.ticks_ms(), self.last_rx_ms) < self.LINK_TIMEOUT_MS

    def send(self, message_type, payload=b"", flags=0):
        frame = encode_frame(message_type, self.sequence, payload, flags)
        self.sequence = (self.sequence + 1) & 0xFF
        try:
            written = self.uart.write(frame)
        except OSError:
            self.tx_errors += 1
            return False
        if written != len(frame):
            self.tx_errors += 1
            return False
        self.last_tx_ms = time.ticks_ms()
        return True

    def send_hello(self):
        return self.send(HELLO, bytes((self.role, PROTOCOL_VERSION)))

    def poll(self):
        waiting = self.uart.any()
        if not waiting:
            return ()
        data = self.uart.read(min(waiting, 1024))
        if not data:
            return ()
        packets = self.parser.feed(data)
        if packets:
            self.last_rx_ms = time.ticks_ms()
        return packets

    def deinit(self):
        self.uart.deinit()
