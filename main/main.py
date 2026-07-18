"""Main-controller service for the OpenKeiko FW1."""

import struct
import time

from fw1_cc1101 import FW1Radios
from fw1_fpga import FW1FPGAClock
from fw1_radio_decode import decode_princeton_frames
from fw1_link import (
    BUTTON_EVENT,
    DISPLAY_STATUS,
    HEARTBEAT,
    IR_NEC_COMMAND,
    IR_RECEIVED,
    MAIN_STATUS,
    RADIO_BAND_RESULT,
    RADIO_CONFIG_RESULT,
    RADIO_CONFIGURE_RX,
    RADIO_DECODED,
    RADIO_PACKET,
    RADIO_RAW_CAPTURE,
    RADIO_SET_BANDS,
    POWER_PREPARE,
    POWER_READY,
    POWER_RESUME,
    PING,
    PONG,
    ROLE_MAIN,
    TONE_COMMAND,
    FW1Link,
)
from fw1_watchdog import feed_watchdog, start_watchdog


class MainController:
    HELLO_MS = 1_000
    STATUS_MS = 1_000
    RADIO_PROBE_MS = 2_000
    RADIO_ROUTE_MS = 5_000

    def __init__(self):
        self.watchdog = start_watchdog()
        now = time.ticks_ms()
        self.started_at = now
        self.link = FW1Link(ROLE_MAIN)
        self.fpga_clock = FW1FPGAClock()
        self.fpga_clock_hz = self.fpga_clock.start(divider=4)
        self.radios = FW1Radios()
        self.radio_status = self.radios.initialize_receive()
        self.display_status = None
        self.next_hello_at = now
        self.next_status_at = now
        self.next_radio_probe_at = time.ticks_add(now, self.RADIO_PROBE_MS)
        self.next_radio_route_at = now
        self.was_connected = False
        self.raw_capture_sequence = 0

    def uptime_ms(self, now=None):
        if now is None:
            now = time.ticks_ms()
        return time.ticks_diff(now, self.started_at) & 0xFFFFFFFF

    def handle_packet(self, packet):
        message_type, sequence, flags, payload = packet
        if message_type == PING:
            self.link.send(PONG, payload)
        elif message_type == DISPLAY_STATUS:
            self.display_status = payload
        elif message_type == BUTTON_EVENT and len(payload) == 2:
            print("display button {} {}".format(payload[0], "down" if payload[1] else "up"))
        elif message_type == IR_RECEIVED and len(payload) == 5:
            protocol, address, command = struct.unpack("<BHH", payload)
            name = "NEC" if protocol == 1 else "NECext"
            print(
                "IR RX {} address=0x{:04x} command=0x{:04x}".format(
                    name, address, command
                )
            )
        elif message_type == IR_RECEIVED and len(payload) == 3:
            address, command = struct.unpack("<HB", payload)
            print("IR RX NEC address=0x{:04x} command=0x{:02x}".format(address, command))
        elif message_type == RADIO_CONFIGURE_RX and len(payload) >= 6:
            radio_mask, preset, frequency_hz = struct.unpack("<BBI", payload[:6])
            try:
                configured_mask, actual_frequency = self.radios.configure_receive(
                    radio_mask, preset, frequency_hz, payload[6:]
                )
                status = 0
            except ValueError as error:
                configured_mask = 0
                actual_frequency = 0
                status = 1
                print("radio RX configuration rejected: {}".format(error))
            except OSError as error:
                configured_mask = 0
                actual_frequency = 0
                status = 2
                print("radio RX configuration failed: {}".format(error))
            self.radio_status = self.radios.probe()
            self.link.send(
                RADIO_CONFIG_RESULT,
                struct.pack(
                    "<BBBBI",
                    status,
                    configured_mask,
                    preset,
                    0,
                    actual_frequency,
                ),
            )
            if status == 0:
                self.send_radio_bands()
                print(
                    "radio RX configured mask=0x{:x} preset={} frequency={} Hz".format(
                        configured_mask, preset, actual_frequency
                    )
                )
        elif message_type == RADIO_BAND_RESULT and len(payload) == 5:
            status, band1, band2, output, configuration = payload
            print(
                "radio filters status={} bands={}/{} PCA=0x{:02x}/0x{:02x}".format(
                    status, band1, band2, output, configuration
                )
            )
        elif message_type == POWER_PREPARE:
            self.radios.prepare_shutdown()
            self.radio_status = self.radios.probe()
            self.link.send(POWER_READY)
            print("display requested power-off preparation")
        elif message_type == POWER_RESUME:
            self.radio_status = self.radios.resume_receive()
            print("display cancelled power-off; radio RX resumed")

    def send_radio_bands(self):
        band1, band2 = self.radios.radio_bands()
        return self.link.send(RADIO_SET_BANDS, bytes((band1, band2)))

    def send_ir_nec(self, address, command):
        return self.link.send(IR_NEC_COMMAND, struct.pack("<HB", address, command))

    def send_tone(self, frequency=880, duration_ms=120, volume_percent=4):
        volume_percent = max(0, min(10, int(volume_percent)))
        return self.link.send(
            TONE_COMMAND,
            struct.pack("<HHB", frequency, duration_ms, volume_percent),
        )

    def update_link(self, now):
        for packet in self.link.poll():
            self.handle_packet(packet)

        connected = self.link.connected
        if connected != self.was_connected:
            print("display link {}".format("up" if connected else "down"))
            self.was_connected = connected
            if connected:
                self.send_radio_bands()
                self.next_radio_route_at = time.ticks_add(now, self.RADIO_ROUTE_MS)

        if connected and time.ticks_diff(now, self.next_radio_route_at) >= 0:
            self.send_radio_bands()
            self.next_radio_route_at = time.ticks_add(now, self.RADIO_ROUTE_MS)

        if time.ticks_diff(now, self.next_hello_at) >= 0:
            self.link.send_hello()
            self.next_hello_at = time.ticks_add(now, self.HELLO_MS)

    def update_radio_packets(self):
        for index, packet in self.radios.poll_packets():
            rssi = max(-128, min(127, packet["rssi_dbm"]))
            flags = 1 if packet["crc_ok"] else 0
            payload = struct.pack(
                "<BbBB",
                index,
                rssi,
                packet["lqi"],
                flags,
            ) + packet["data"]
            self.link.send(RADIO_PACKET, payload)
            print(
                "radio{} RX {} bytes RSSI={} dBm LQI={} CRC={}".format(
                    index + 1,
                    len(packet["data"]),
                    packet["rssi_dbm"],
                    packet["lqi"],
                    packet["crc_ok"],
                )
            )

    def update_raw_captures(self):
        for radio, timings, truncated in self.radios.poll_raw_captures():
            decoded = decode_princeton_frames(timings)
            capture_id = self.raw_capture_sequence
            self.raw_capture_sequence = (capture_id + 1) & 0xFFFF
            chunk_index = 0
            for offset in range(0, len(timings), 120):
                chunk = timings[offset : offset + 120]
                final = offset + len(chunk) >= len(timings)
                flags = (1 if final else 0) | (2 if truncated else 0)
                payload = bytearray(
                    struct.pack(
                        "<HBBBB",
                        capture_id,
                        radio,
                        chunk_index,
                        flags,
                        len(chunk),
                    )
                )
                for duration in chunk:
                    payload.extend(struct.pack("<i", duration))
                self.link.send(RADIO_RAW_CAPTURE, payload)
                chunk_index += 1
            print(
                "radio{} RAW {} timings{}".format(
                    radio + 1,
                    len(timings),
                    " truncated" if truncated else "",
                )
            )
            if decoded:
                first = decoded[0]
                repeats = sum(frame["key"] == first["key"] for frame in decoded)
                self.link.send(
                    RADIO_DECODED,
                    struct.pack(
                        "<BBBBIH",
                        radio,
                        1,
                        first["bits"],
                        min(255, repeats),
                        first["key"],
                        first["te_us"],
                    ),
                )
                print(
                    "radio{} Princeton key={:06X} TE={} us repeats={}".format(
                        radio + 1,
                        first["key"],
                        first["te_us"],
                        repeats,
                    )
                )

    def update_radios(self, now):
        if time.ticks_diff(now, self.next_radio_probe_at) < 0:
            return
        self.radios.maintain_receive()
        self.radio_status = self.radios.probe()
        self.next_radio_probe_at = time.ticks_add(now, self.RADIO_PROBE_MS)

    def send_status(self, now):
        if time.ticks_diff(now, self.next_status_at) < 0:
            return

        present_mask = 0
        fields = []
        for index, status in enumerate(self.radio_status):
            if status["present"]:
                present_mask |= 1 << index
            fields.extend((
                status["part"],
                status["version"],
                status["marcstate"],
                status["gdo0"],
                status["gdo2"],
            ))
        rssi = []
        for status in self.radio_status:
            value = status.get("rssi_dbm")
            rssi.append(-128 if value is None else max(-127, min(127, value)))
        payload = struct.pack(
            "<IB10Bbb",
            self.uptime_ms(now),
            present_mask,
            *fields,
            *rssi
        )
        self.link.send(MAIN_STATUS, payload)
        self.link.send(HEARTBEAT, struct.pack("<I", self.uptime_ms(now)))
        self.next_status_at = time.ticks_add(now, self.STATUS_MS)

    def run(self):
        print("OpenKeiko main controller started")
        print("FPGA clock {} Hz on GPIO23".format(self.fpga_clock_hz))
        for index, status in enumerate(self.radio_status, 1):
            print(
                "CC1101 #{} present={} rx={} part=0x{:02x} version=0x{:02x}".format(
                    index,
                    status["present"],
                    status["receive"],
                    status["part"],
                    status["version"],
                )
            )

        try:
            while True:
                feed_watchdog(self.watchdog)
                now = time.ticks_ms()
                self.update_link(now)
                self.update_radio_packets()
                self.update_raw_captures()
                self.update_radios(now)
                self.send_status(now)
                time.sleep_ms(2)
        finally:
            self.radios.deinit()
            self.link.deinit()


MainController().run()
