"""Interactive hardware dashboard for the OpenKeiko FW1 display RP2040."""

from machine import Pin
import os
import struct
import time

import st7789py as st7789
import vga1_8x8 as font_small
import vga1_8x16 as font_status
import vga1_bold_16x16 as font_title

from fw1_audio import PDMLevel, Speaker
from fw1_effects import FW1Effects, MODE_PRIDE_2015
from fw1_i2c import FW1I2C
from fw1_flipper import (
    IR_FILETYPE,
    SUB_RAW_FILETYPE,
    format_ir,
    format_sub,
    load_ir,
    parse_sub,
)
from fw1_ir import IRReceiver, IRTransmitter, decode_nec_timings
from fw1_link import (
    BUTTON_EVENT,
    DISPLAY_STATUS,
    HEARTBEAT,
    HELLO,
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
    ROLE_DISPLAY,
    TONE_COMMAND,
    FW1Link,
)
from fw1_tft import FW1TFT
from fw1_watchdog import feed_watchdog, start_watchdog
from ws2812 import WS2812


SUB_PRESET_IDS = {
    "FuriHalSubGhzPresetOok270Async": 1,
    "FuriHalSubGhzPresetOok650Async": 2,
    "FuriHalSubGhzPreset2FSKDev238Async": 3,
    "FuriHalSubGhzPreset2FSKDev12KAsync": 4,
    "FuriHalSubGhzPreset2FSKDev476Async": 5,
    "FuriHalSubGhzPresetCustom": 255,
}
SUB_PRESET_NAMES = {
    0: "OpenKeikoPacket2FSK",
    1: "FuriHalSubGhzPresetOok270Async",
    2: "FuriHalSubGhzPresetOok650Async",
    3: "FuriHalSubGhzPreset2FSKDev238Async",
    4: "FuriHalSubGhzPreset2FSKDev12KAsync",
    5: "FuriHalSubGhzPreset2FSKDev476Async",
    255: "FuriHalSubGhzPresetCustom",
}


class DebouncedButton:
    def __init__(self, name, gpio, color, now):
        self.name = name
        self.gpio = gpio
        self.color = color
        self.pin = Pin(gpio, Pin.IN, Pin.PULL_UP)
        self.raw = self.pin.value()
        self.stable = self.raw
        self.changed_at = now

    @property
    def pressed(self):
        return self.stable == 0

    def update(self, now, debounce_ms):
        current = self.pin.value()
        if current != self.raw:
            self.raw = current
            self.changed_at = now
            return False

        if (
            current != self.stable
            and time.ticks_diff(now, self.changed_at) >= debounce_ms
        ):
            self.stable = current
            return True

        return False


class TestUI:
    BG = st7789.color565(7, 15, 20)
    SURFACE = st7789.color565(15, 29, 36)
    SURFACE_2 = st7789.color565(9, 20, 26)
    GRID = st7789.color565(39, 58, 65)
    TEXT = st7789.color565(232, 243, 239)
    MUTED = st7789.color565(121, 145, 147)
    CYAN = st7789.color565(52, 207, 205)
    GREEN = st7789.color565(54, 214, 126)
    AMBER = st7789.color565(245, 180, 72)
    INK = st7789.color565(5, 13, 17)
    ERROR = st7789.color565(240, 93, 94)

    BUTTON_X = 8
    BUTTON_Y = 68
    BUTTON_W = 58
    BUTTON_H = 42
    BUTTON_STEP = 62
    PANEL_Y = 118
    PANEL_H = 114
    SERVICE_X = 164
    SERVICE_W = 50
    FX_X = 222
    FX_W = 90

    def __init__(self, panel, buttons, rgb_enabled, effect_mode):
        self.panel = panel
        self.buttons = buttons
        self._accel_state = ()
        self._rtc_state = ()
        self._power_state = ()
        self._service_state = ()
        self.draw_static()
        self.draw_services(False, 0, False, 0)
        self.draw_rgb(rgb_enabled, effect_mode)
        for index, button in enumerate(buttons):
            self.draw_button(index, button.pressed)
        self.draw_accelerometer(None)
        self.draw_rtc(None)
        self.draw_power(None, 0, None)

    @staticmethod
    def _center(text, width, glyph_width):
        return max(0, (width - len(text) * glyph_width) // 2)

    def draw_static(self):
        tft = self.panel
        tft.fill(self.BG)
        tft.fill_rect(0, 0, 5, 48, self.CYAN)
        tft.text(font_title, "OPENKEIKO", 14, 7, self.TEXT, self.BG)
        tft.text(font_small, "HW MATRIX // FW1", 14, 29, self.MUTED, self.BG)
        tft.fill_rect(self.SERVICE_X, 9, self.SERVICE_W, 30, self.SURFACE_2)
        tft.rect(self.SERVICE_X, 9, self.SERVICE_W, 30, self.GRID)
        tft.fill_rect(0, 47, 320, 2, self.GRID)
        tft.text(font_small, "INPUTS", 8, 56, self.CYAN, self.BG)
        tft.fill_rect(8, 114, 304, 1, self.GRID)

        self._panel_frame(8, self.PANEL_Y, 96, self.PANEL_H, "ACCEL", self.CYAN)
        self._panel_frame(112, self.PANEL_Y, 94, self.PANEL_H, "RTC", self.AMBER)
        self._panel_frame(214, self.PANEL_Y, 98, self.PANEL_H, "POWER+I2C", self.GREEN)

    def _panel_frame(self, x, y, width, height, title, color):
        self.panel.fill_rect(x, y, width, height, self.SURFACE)
        self.panel.rect(x, y, width, height, self.GRID)
        self.panel.fill_rect(x, y, width, 3, color)
        self.panel.text(font_small, title, x + 6, y + 8, color, self.SURFACE)

    def _clear_panel_body(self, x, y, width):
        self.panel.fill_rect(x + 5, y + 25, width - 10, self.PANEL_H - 30, self.SURFACE)

    def draw_services(self, linked, radio_mask, ir_active, mic_level):
        state = (linked, radio_mask, ir_active, mic_level // 10)
        if state == self._service_state:
            return
        self._service_state = state

        x, y, width, height = self.SERVICE_X, 9, self.SERVICE_W, 30
        border = self.AMBER if ir_active else (self.GREEN if linked else self.GRID)
        self.panel.fill_rect(x, y, width, height, self.SURFACE_2)
        self.panel.rect(x, y, width, height, border)
        link = "M{} R{:X}".format("+" if linked else "-", radio_mask & 0x03)
        mic = "MIC{:02d}".format(min(99, mic_level))
        self.panel.text(font_small, link, x + 5, y + 4, border, self.SURFACE_2)
        self.panel.text(font_small, mic, x + 5, y + 17, self.MUTED, self.SURFACE_2)

    def draw_power_action(
        self,
        heading,
        detail,
        countdown=None,
        error=False,
        footer="RELEASE RED TO CANCEL",
    ):
        color = self.ERROR if error else self.AMBER
        self.panel.fill(self.BG)
        self.panel.fill_rect(0, 0, 320, 6, color)
        title_x = self._center(heading, 320, 16)
        self.panel.text(font_title, heading, title_x, 52, color, self.BG)
        detail_x = self._center(detail, 320, 8)
        self.panel.text(font_status, detail, detail_x, 94, self.TEXT, self.BG)
        if countdown is not None:
            value = str(countdown)
            value_x = self._center(value, 320, 16)
            self.panel.text(font_title, value, value_x, 132, self.TEXT, self.BG)
        self.panel.text(
            font_small,
            footer,
            self._center(footer, 320, 8),
            190,
            self.MUTED,
            self.BG,
        )

    def draw_rgb(self, enabled, effect_mode=0):
        x, y, width, height = self.FX_X, 9, self.FX_W, 30
        background = self.GREEN if enabled else self.SURFACE
        foreground = self.INK if enabled else self.MUTED
        label = "FX {:02d}".format(effect_mode) if enabled else "RGB OFF"

        self.panel.fill_rect(x, y, width, height, background)
        self.panel.rect(x, y, width, height, self.GREEN if enabled else self.GRID)
        text_x = x + self._center(label, width, 8)
        self.panel.text(font_status, label, text_x, y + 7, foreground, background)

    def draw_button(self, index, pressed):
        button = self.buttons[index]
        x = self.BUTTON_X + index * self.BUTTON_STEP
        y = self.BUTTON_Y
        width = self.BUTTON_W
        height = self.BUTTON_H
        background = button.color if pressed else self.SURFACE
        foreground = self.INK if pressed else self.TEXT

        self.panel.fill_rect(x, y, width, height, background)
        self.panel.rect(x, y, width, height, button.color)
        self.panel.fill_rect(x, y, width, 3, button.color)

        label_x = x + self._center(button.name, width, 8)
        self.panel.text(font_small, button.name, label_x, y + 5, foreground, background)

        state = "DOWN" if pressed else "UP"
        state_x = x + self._center(state, width, 8)
        self.panel.text(
            font_small,
            state,
            state_x,
            y + 17,
            self.INK if pressed else button.color,
            background,
        )

        gpio = "GP{}".format(button.gpio)
        gpio_x = x + self._center(gpio, width, 8)
        self.panel.text(font_small, gpio, gpio_x, y + 29, foreground, background)

    def draw_accelerometer(self, values):
        state = tuple(values) if values is not None else None
        if state == self._accel_state:
            return
        self._accel_state = state

        x, y, width = 8, self.PANEL_Y, 96
        self._clear_panel_body(x, y, width)
        if values is None:
            self.panel.text(font_small, "NO DATA", x + 12, y + 47, self.ERROR, self.SURFACE)
            return

        for row, (axis, value) in enumerate(zip("XYZ", values)):
            text = "{} {:+d}".format(axis, value)
            self.panel.text(
                font_small,
                text,
                x + 8,
                y + 32 + row * 20,
                self.TEXT if axis != "Z" else self.CYAN,
                self.SURFACE,
            )

    def draw_rtc(self, values):
        state = tuple(values) if values is not None else None
        if state == self._rtc_state:
            return
        self._rtc_state = state

        x, y, width = 112, self.PANEL_Y, 94
        self._clear_panel_body(x, y, width)
        if values is None:
            self.panel.text(font_small, "NO RTC", x + 19, y + 47, self.ERROR, self.SURFACE)
            return

        running, year, month, day, hour, minute, second = values
        clock = "{:02d}:{:02d}:{:02d}".format(hour, minute, second)
        date = "{:04d}-{:02d}-{:02d}".format(year, month, day)
        status = "RUNNING" if running else "STOPPED"

        self.panel.text(font_small, clock, x + 15, y + 32, self.TEXT, self.SURFACE)
        self.panel.text(font_small, date, x + 7, y + 52, self.MUTED, self.SURFACE)
        self.panel.text(
            font_small,
            status,
            x + self._center(status, width, 8),
            y + 76,
            self.GREEN if running else self.ERROR,
            self.SURFACE,
        )

    def draw_power(self, charger, device_count, auxiliary):
        aux_hex = auxiliary.hex().upper() if auxiliary is not None else "----"
        if charger is None:
            state = (None, device_count, aux_hex)
        else:
            state = (
                charger["source"],
                charger["charge"],
                charger["battery_mv"],
                charger["charge_ma"],
                device_count,
                aux_hex,
            )
        if state == self._power_state:
            return
        self._power_state = state

        x, y, width = 214, self.PANEL_Y, 98
        self._clear_panel_body(x, y, width)
        if charger is None:
            self.panel.text(font_small, "NO CHARGER", x + 9, y + 39, self.ERROR, self.SURFACE)
            return

        battery_mv = charger["battery_mv"]
        battery = "BAT {}.{:02d}V".format(
            battery_mv // 1000,
            (battery_mv % 1000) // 10,
        )
        current = "{} {}mA".format(charger["charge"], charger["charge_ma"])
        aux = "{}/4 IOX{}".format(device_count, aux_hex)

        self.panel.text(font_small, charger["source"], x + 7, y + 29, self.TEXT, self.SURFACE)
        self.panel.text(font_small, current, x + 7, y + 47, self.GREEN, self.SURFACE)
        self.panel.text(font_small, battery, x + 7, y + 65, self.AMBER, self.SURFACE)
        self.panel.text(font_small, aux, x + 7, y + 83, self.MUTED, self.SURFACE)

    def _app_header(self, title, subtitle, color):
        self.panel.fill(self.BG)
        self.panel.fill_rect(0, 0, 6, 48, color)
        self.panel.text(font_title, title, 15, 7, self.TEXT, self.BG)
        self.panel.text(font_small, subtitle, 15, 30, self.MUTED, self.BG)
        self.panel.fill_rect(0, 48, 320, 2, self.GRID)

    def _footer(self, left="GRAY BACK", center="Y/B MOVE", right="GRN OPEN"):
        self.panel.fill_rect(0, 217, 320, 23, self.SURFACE_2)
        self.panel.fill_rect(0, 217, 320, 1, self.GRID)
        self.panel.text(font_small, left, 8, 225, self.MUTED, self.SURFACE_2)
        self.panel.text(font_small, center, 116, 225, self.MUTED, self.SURFACE_2)
        self.panel.text(font_small, right, 232, 225, self.MUTED, self.SURFACE_2)

    def draw_menu(self, items, selected):
        self._app_header("DEVICE MENU", "OPERATE // INSPECT // CAPTURE", self.CYAN)
        for index, item in enumerate(items):
            y = 61 + index * 47
            active = index == selected
            background = self.CYAN if active else self.SURFACE
            foreground = self.INK if active else self.TEXT
            self.panel.fill_rect(14, y, 292, 38, background)
            self.panel.rect(14, y, 292, 38, self.CYAN if active else self.GRID)
            self.panel.text(font_status, item, 27, y + 11, foreground, background)
            marker = ">" if active else "-"
            self.panel.text(font_status, marker, 281, y + 11, foreground, background)
        self._footer(left="GRAY HOME")

    def draw_ir_app(
        self,
        selected,
        total,
        label,
        detail,
        last_nec,
        last_protocol,
        raw_count,
        notice,
    ):
        self._app_header("INFRARED", "GPIO16 RX // GPIO9 TX", self.AMBER)
        self.panel.text(font_small, "SIGNAL {:02d}/{:02d}".format(selected + 1, total), 14, 60, self.AMBER, self.BG)
        self.panel.fill_rect(14, 75, 292, 38, self.SURFACE)
        self.panel.rect(14, 75, 292, 38, self.AMBER)
        self.panel.text(font_status, label[:32], 22, 86, self.TEXT, self.SURFACE)
        self.panel.text(font_small, detail[:38], 16, 123, self.MUTED, self.BG)
        if last_nec is None:
            decoded = "NEC/NECext  WAITING"
        else:
            decoded = "{} A:{:04X} C:{:04X}".format(
                last_protocol or "NEC?",
                last_nec[0],
                last_nec[1],
            )
        self.panel.text(font_status, decoded, 16, 147, self.GREEN, self.BG)
        self.panel.text(font_small, "RAW {:03d} EDGES".format(raw_count), 16, 171, self.CYAN, self.BG)
        self.panel.text(font_small, notice[:38], 16, 194, self.ERROR if notice.startswith("ERR") else self.MUTED, self.BG)
        action = "GRN SAVE" if selected == 0 else "GRN SEND"
        self._footer(right=action)

    def draw_subghz_app(
        self,
        selected,
        total,
        filename,
        summary,
        packet,
        radio_mask,
        radio_rssi,
        raw_count,
        raw_radio,
        notice,
        action,
    ):
        self._app_header("SUB-GHZ", "DUAL CC1101 // RX-ONLY SAFE MODE", self.GREEN)
        self.panel.text(
            font_small,
            "R{:X} RSSI {}/{} PKT {}".format(
                radio_mask & 3,
                radio_rssi[0],
                radio_rssi[1],
                packet[0],
            ),
            14,
            60,
            self.GREEN,
            self.BG,
        )
        self.panel.fill_rect(14, 76, 292, 38, self.SURFACE)
        self.panel.rect(14, 76, 292, 38, self.GREEN)
        position = "{:02d}/{:02d}".format(selected + 1, total) if total else "00/00"
        self.panel.text(font_small, position, 22, 82, self.MUTED, self.SURFACE)
        self.panel.text(font_status, filename[:26], 65, 87, self.TEXT, self.SURFACE)
        for row, text in enumerate(summary[:3]):
            self.panel.text(font_small, text[:38], 16, 123 + row * 17, self.MUTED, self.BG)
        if raw_count:
            packet_text = "RAW R{} {} EDGES{}".format(
                raw_radio + 1,
                raw_count,
                " !" if "TRUNC" in notice else "",
            )
        elif packet[0]:
            packet_text = "R{} {}B {}dBm {}".format(packet[1], packet[2], packet[3], packet[4])
        else:
            packet_text = "NO PACKETS YET"
        self.panel.text(font_small, packet_text[:38], 16, 177, self.CYAN, self.BG)
        self.panel.text(font_small, notice[:38], 16, 197, self.AMBER, self.BG)
        self._footer(right=action)


class DisplayController:
    LED_PIN = 7
    LED_COUNT = 7
    LED_BRIGHTNESS = 0.08
    TFT_BRIGHTNESS = 0.65
    BUTTON_DEBOUNCE_MS = 35
    ACCEL_UPDATE_MS = 200
    STATUS_UPDATE_MS = 1_000
    I2C_SCAN_MS = 5_000
    LINK_STATUS_MS = 1_000
    SERVICE_UPDATE_MS = 100
    RED_POWER_HOLD_MS = 3_000
    POWER_READY_TIMEOUT_MS = 750

    BUTTON_CONFIG = (
        ("GRAY", 14, st7789.color565(158, 172, 176)),
        ("YEL", 15, st7789.color565(245, 207, 70)),
        ("GRN", 22, st7789.color565(62, 214, 123)),
        ("BLUE", 23, st7789.color565(55, 168, 241)),
        ("RED", 24, st7789.color565(240, 93, 94)),
    )

    def __init__(self):
        self.watchdog = start_watchdog()
        now = time.ticks_ms()
        self.started_at = now
        self.buttons = [
            DebouncedButton(name, gpio, color, now)
            for name, gpio, color in self.BUTTON_CONFIG
        ]

        self.link = FW1Link(ROLE_DISPLAY)
        self.ir_receiver = IRReceiver()
        self.ir_transmitter = IRTransmitter()
        try:
            self.microphone = PDMLevel(state_machine=2)
        except (OSError, ValueError) as error:
            print("PDM microphone unavailable: {}".format(error))
            self.microphone = None
        self.speaker = None
        self.main_radio_mask = 0
        self.main_radio_rssi = (-128, -128)
        self.radio_packet_count = 0
        self.last_acceleration = (0, 0, 0)
        self.last_battery_mv = 0
        self.mic_level = 0
        self.ir_activity = self.ir_receiver.activity
        self.ir_active_until = now
        self.last_ir_nec = None
        self.last_ir_protocol = None
        self.last_ir_raw = None
        self.last_ir_capture_parsed = None
        self.ir_decoded_since_capture = False
        self.menu_items = ("INFRARED", "SUB-GHZ", "STATUS")
        self.view = "status"
        self.menu_index = 0
        self.ir_index = 0
        self.ir_entries = []
        self.ir_notice = "LIVE RECEIVE READY"
        self.sub_index = 0
        self.sub_files = []
        self.sub_document = None
        self.sub_summary = ("DEFAULT PACKET RX", "433.92 MHz 2-FSK", "GRN RESTORES DEFAULT")
        self.sub_notice = "RX-ONLY // PATABLE ZERO"
        self.sub_armed_index = None
        self.sub_pending_index = None
        self.sub_pending_custom = b""
        self.sub_active_preset = 0
        self.sub_active_custom = b""
        self.sub_active_frequency = 433_919_830
        self.sub_capture_id = None
        self.sub_capture_radio = 0
        self.sub_capture_parts = []
        self.sub_capture_next_chunk = 0
        self.last_sub_raw = None
        self.last_sub_truncated = False
        self.last_sub_decoded = None
        self.last_radio_packet = (0, 0, 0, 0, "")
        self.power_state = None
        self.power_hold_started = None
        self.power_countdown = None
        self.power_ready = False
        self.power_deadline = now
        self.power_loss_deadline = now
        self.power_latched = False

        self.i2c = FW1I2C()
        self.tft = FW1TFT(brightness=self.TFT_BRIGHTNESS)
        self.leds = WS2812(
            pin=self.LED_PIN,
            count=self.LED_COUNT,
            brightness=self.LED_BRIGHTNESS,
            inverted=True,
            state_machine=0,
        )

        self.effects = FW1Effects(
            self.leds,
            mode=MODE_PRIDE_2015,
            speed=160,
            brightness=self.LED_BRIGHTNESS,
        )
        self.leds_enabled = not self.buttons[3].pressed
        if not self.leds_enabled:
            self.leds.off()

        self.ui = TestUI(
            self.tft.panel,
            self.buttons,
            self.leds_enabled,
            self.effects.mode,
        )
        self.next_accel_at = now
        self.next_status_at = now
        self.next_scan_at = now
        self.next_link_status_at = now
        self.next_service_at = now
        self.update_i2c(now)

    def set_leds_enabled(self, enabled):
        self.leds_enabled = bool(enabled)
        if self.leds_enabled:
            self.effects.set_mode(self.effects.mode)
        else:
            self.leds.off()
        self.ui.draw_rgb(self.leds_enabled, self.effects.mode)
        print("RGB {}".format("on" if self.leds_enabled else "off"))

    def uptime_ms(self, now=None):
        if now is None:
            now = time.ticks_ms()
        return time.ticks_diff(now, self.started_at) & 0xFFFFFFFF

    @staticmethod
    def _files(directory, suffix):
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return []
        return [directory + "/" + name for name in names if name.lower().endswith(suffix)]

    def refresh_ir_files(self):
        entries = [{"label": "LIVE CAPTURE", "detail": "GRN SAVES LAST RAW", "signal": None}]
        for path in self._files("/infrared", ".ir")[:16]:
            try:
                if os.stat(path)[6] > 48 * 1024:
                    continue
                document = load_ir(path)
            except (OSError, ValueError):
                continue
            filename = path.rsplit("/", 1)[-1]
            for signal in document["signals"]:
                if len(entries) >= 48:
                    break
                detail = signal.get("protocol", "RAW {}Hz".format(signal.get("frequency", 0)))
                entries.append(
                    {
                        "label": "{}:{}".format(filename[:-3], signal["name"]),
                        "detail": "{} // {}".format(signal["type"].upper(), detail),
                        "signal": signal,
                    }
                )
        self.ir_entries = entries
        self.ir_index %= len(entries)

    def refresh_sub_files(self):
        self.sub_files = self._files("/subghz", ".sub")[:32]
        self.sub_index %= len(self.sub_files) + 1
        self.load_sub_summary()

    def load_sub_summary(self):
        self.sub_document = None
        if self.sub_index == 0:
            self.sub_summary = (
                "DEFAULT PACKET RX",
                "433.92 MHz 2-FSK",
                "GRN RESTORES DEFAULT",
            )
            return
        path = self.sub_files[self.sub_index - 1]
        try:
            size = os.stat(path)[6]
            if size > 64 * 1024:
                raise ValueError("FILE >64K; HOST ONLY")
            with open(path, "r") as source:
                document = parse_sub(source)
            self.sub_document = document
            protocol = document["protocol"]
            preset = document["preset"].replace("FuriHalSubGhzPreset", "")
            if protocol == "RAW":
                detail = "RAW {} TIMINGS".format(len(document["raw_data"]))
            elif "bit" in document:
                detail = "{} {} BIT".format(protocol, document["bit"])
            else:
                detail = protocol
            capability = "GRN ARM RX" if document["preset"] in SUB_PRESET_IDS else "PRESET UNSUPPORTED"
            self.sub_summary = (
                "{:.5f} MHz".format(document["frequency"] / 1_000_000),
                preset,
                detail + " // " + capability,
            )
        except (OSError, ValueError) as error:
            self.sub_summary = ("ERR .SUB", str(error), "RX UNCHANGED")

    def draw_current_view(self, now=None):
        if now is None:
            now = time.ticks_ms()
        if self.view == "status":
            self.redraw_dashboard(now)
        elif self.view == "menu":
            self.ui.draw_menu(self.menu_items, self.menu_index)
        elif self.view == "ir":
            entry = self.ir_entries[self.ir_index]
            self.ui.draw_ir_app(
                self.ir_index,
                len(self.ir_entries),
                entry["label"],
                entry["detail"],
                self.last_ir_nec,
                self.last_ir_protocol,
                len(self.last_ir_raw) if self.last_ir_raw else 0,
                self.ir_notice,
            )
        elif self.view == "subghz":
            filename = (
                "DEFAULT PACKET RX"
                if self.sub_index == 0
                else self.sub_files[self.sub_index - 1].rsplit("/", 1)[-1]
            )
            if self.last_sub_raw and self.sub_armed_index == self.sub_index:
                action = "GRN SAVE"
            elif self.sub_pending_index is not None:
                action = "GRN WAIT"
            else:
                action = "GRN ARM"
            self.ui.draw_subghz_app(
                self.sub_index,
                len(self.sub_files) + 1,
                filename,
                self.sub_summary,
                self.last_radio_packet,
                self.main_radio_mask,
                self.main_radio_rssi,
                len(self.last_sub_raw) if self.last_sub_raw else 0,
                self.sub_capture_radio,
                self.sub_notice,
                action,
            )

    def open_view(self, view, now):
        self.view = view
        if view == "ir":
            self.refresh_ir_files()
        elif view == "subghz":
            self.refresh_sub_files()
        self.draw_current_view(now)

    def save_last_ir(self):
        if not self.last_ir_raw:
            self.ir_notice = "ERR NO COMPLETE RAW FRAME"
            return
        try:
            try:
                os.mkdir("/infrared")
            except OSError:
                pass
            existing = os.listdir("/infrared")
            capture_index = 1
            for existing_name in existing:
                if existing_name.startswith("capture_") and existing_name.endswith(".ir"):
                    try:
                        capture_index = max(
                            capture_index,
                            int(existing_name[8:-3]) + 1,
                        )
                    except ValueError:
                        pass
            name = "capture_{:04d}.ir".format(capture_index)
            path = "/infrared/" + name
            signal = {
                "name": "Capture_{:04d}".format(capture_index),
            }
            if self.last_ir_capture_parsed is not None:
                protocol, address, command = self.last_ir_capture_parsed
                signal.update(
                    type="parsed",
                    protocol=protocol,
                    address=address,
                    command=command,
                )
                saved_type = protocol
            else:
                signal.update(
                    type="raw",
                    frequency=38_000,
                    duty_cycle=0.33,
                    data=self.last_ir_raw,
                )
                saved_type = "RAW"
            document = {"filetype": IR_FILETYPE, "signals": (signal,)}
            with open(path, "w") as output:
                output.write(format_ir(document))
            self.ir_notice = "SAVED {} {}".format(saved_type, name)
            self.refresh_ir_files()
        except (OSError, ValueError) as error:
            self.ir_notice = "ERR {}".format(error)

    def activate_ir_entry(self):
        entry = self.ir_entries[self.ir_index]
        signal = entry["signal"]
        if signal is None:
            self.save_last_ir()
            return

        self.ir_receiver.pause()
        try:
            if signal["type"] == "raw":
                normalized = decode_nec_timings(signal["data"])
                if normalized is None:
                    self.ir_transmitter.send_raw(
                        signal["data"], signal["frequency"], signal["duty_cycle"]
                    )
                    self.ir_notice = "SENT RAW // {} EDGES".format(
                        len(signal["data"])
                    )
                else:
                    protocol, address, command = normalized
                    if protocol == "NEC":
                        self.ir_transmitter.send_nec(address, command)
                    else:
                        self.ir_transmitter.send_nec_ext(address, command)
                    self.ir_notice = "SENT RAW->{} A{:04X} C{:04X}".format(
                        protocol, address, command
                    )
            elif signal["protocol"] == "NEC":
                self.ir_transmitter.send_nec(signal["address"], signal["command"])
                self.ir_notice = "SENT NEC A{:02X} C{:02X}".format(
                    signal["address"], signal["command"]
                )
            elif signal["protocol"] == "NECext":
                self.ir_transmitter.send_nec_ext(signal["address"], signal["command"])
                self.ir_notice = "SENT NECext A{:04X} C{:04X}".format(
                    signal["address"], signal["command"]
                )
            else:
                raise ValueError("{} TX unsupported".format(signal["protocol"]))
        except (OSError, ValueError) as error:
            self.ir_notice = "ERR {}".format(error)
        finally:
            # Let the demodulator recover from local optical saturation before
            # accepting a new external frame.
            time.sleep_ms(20)
            self.ir_receiver.resume()
            self.ir_activity = self.ir_receiver.activity

    def configure_selected_sub_rx(self):
        if self.sub_index == 0:
            preset = 0
            frequency = 433_920_000
            custom = b""
        elif self.sub_document is None:
            self.sub_notice = "ERR INVALID .SUB"
            return
        else:
            try:
                preset = SUB_PRESET_IDS[self.sub_document["preset"]]
            except KeyError:
                self.sub_notice = "ERR PRESET UNSUPPORTED"
                return
            frequency = self.sub_document["frequency"]
            custom = self.sub_document.get("custom_preset_data", b"")
        if len(custom) > 506:
            self.sub_notice = "ERR CUSTOM PRESET TOO LARGE"
            return
        payload = struct.pack("<BBI", 0x03, preset, frequency) + custom
        if self.link.send(RADIO_CONFIGURE_RX, payload):
            self.sub_pending_index = self.sub_index
            self.sub_pending_custom = custom
            self.sub_notice = "CONFIGURING BOTH RADIOS"
        else:
            self.sub_notice = "ERR MAIN LINK UNAVAILABLE"

    def save_last_sub_capture(self):
        if not self.last_sub_raw:
            self.sub_notice = "ERR NO RAW CAPTURE"
            return
        try:
            preset_name = SUB_PRESET_NAMES[self.sub_active_preset]
            document = {
                "filetype": SUB_RAW_FILETYPE,
                "frequency": self.sub_active_frequency,
                "preset": preset_name,
                "protocol": "RAW",
                "raw_data": self.last_sub_raw,
            }
            if self.sub_active_preset == 255:
                document["custom_preset_module"] = "CC1101"
                document["custom_preset_data"] = self.sub_active_custom
            capture_index = self.sub_capture_id
            while True:
                name = "capture_{:04d}_r{}.sub".format(
                    capture_index,
                    self.sub_capture_radio + 1,
                )
                if name not in os.listdir("/subghz"):
                    break
                capture_index = (capture_index + 1) & 0xFFFF
            with open("/subghz/" + name, "w") as output:
                output.write(format_sub(document))
            self.sub_notice = "SAVED " + name
            self.last_sub_raw = None
            self.refresh_sub_files()
        except (OSError, ValueError, KeyError) as error:
            self.sub_notice = "ERR {}".format(error)

    def activate_sub_entry(self):
        if self.last_sub_raw and self.sub_armed_index == self.sub_index:
            self.save_last_sub_capture()
        else:
            self.configure_selected_sub_rx()

    def play_tone(self, frequency=880, duration_ms=120, volume=0.04):
        try:
            if self.speaker is None:
                self.speaker = Speaker()
            self.speaker.tone(frequency, duration_ms, volume)
            print("speaker {} Hz {} ms".format(frequency, duration_ms))
            return True
        except (OSError, ValueError) as error:
            print("speaker unavailable: {}".format(error))
            if self.speaker is not None:
                self.speaker.deinit()
                self.speaker = None
            return False

    def redraw_dashboard(self, now):
        self.ui = TestUI(
            self.tft.panel,
            self.buttons,
            self.leds_enabled,
            self.effects.mode,
        )
        self.next_accel_at = now
        self.next_status_at = now
        self.next_scan_at = now
        self.next_service_at = now
        self.update_i2c(now)

    def begin_power_hold(self, now):
        if self.power_latched:
            return
        self.power_state = "holding"
        self.power_hold_started = now
        self.power_countdown = 3
        self.ui.draw_power_action("POWER OFF", "HOLD RED", 3)

    def cancel_power_hold(self, now):
        if self.power_state == "preparing":
            self.link.send(POWER_RESUME)
        self.power_state = None
        self.power_hold_started = None
        self.power_countdown = None
        self.power_ready = False
        self.power_latched = False
        self.draw_current_view(now)

    def update_power_control(self, now):
        if self.power_state == "powering_off":
            if time.ticks_diff(now, self.power_loss_deadline) < 0:
                return
            self.power_state = "error"
            self.link.send(POWER_RESUME)
            self.ui.draw_power_action(
                "POWER ERROR",
                "SYS POWER REMAINED ON",
                error=True,
                footer="RELEASE RED TO RETURN",
            )
            return

        if self.power_state == "holding":
            elapsed = time.ticks_diff(now, self.power_hold_started)
            if elapsed < self.RED_POWER_HOLD_MS:
                countdown = 3 - elapsed // 1_000
                if countdown != self.power_countdown:
                    self.power_countdown = countdown
                    self.ui.draw_power_action("POWER OFF", "HOLD RED", countdown)
                return

            self.power_latched = True
            try:
                charger = self.i2c.charger()
            except OSError as error:
                self.power_state = "error"
                self.ui.draw_power_action(
                    "POWER ERROR",
                    "CHARGER UNAVAILABLE",
                    error=True,
                    footer="RELEASE RED TO RETURN",
                )
                print("power-off refused: {}".format(error))
                return

            if self.i2c.has_external_power(charger):
                self.power_state = "usb"
                self.ui.draw_power_action(
                    "USB POWERED",
                    "UNPLUG TO POWER OFF",
                    footer="RELEASE RED TO RETURN",
                )
                print("power-off refused while external power is present")
                return

            self.power_state = "preparing"
            self.power_ready = False
            self.power_deadline = time.ticks_add(now, self.POWER_READY_TIMEOUT_MS)
            self.ui.draw_power_action(
                "POWER OFF",
                "PREPARING",
                footer="RELEASE RED TO CANCEL",
            )
            self.link.send(POWER_PREPARE)
            return

        if self.power_state != "preparing":
            return
        if not self.power_ready and time.ticks_diff(now, self.power_deadline) < 0:
            return

        self.ui.draw_power_action(
            "POWER OFF",
            "DISCONNECTING BATTERY",
            footer="HOLD GRAY TO WAKE",
        )
        self.leds.off()
        time.sleep_ms(60)
        try:
            value = self.i2c.enter_ship_mode()
            print("BQ25892 ship mode requested, REG09=0x{:02x}".format(value))
            self.power_state = "powering_off"
            self.power_loss_deadline = time.ticks_add(now, 1_000)
        except OSError as error:
            self.power_state = "error"
            self.link.send(POWER_RESUME)
            self.ui.draw_power_action(
                "POWER ERROR",
                "SHIP MODE FAILED",
                error=True,
                footer="RELEASE RED TO RETURN",
            )
            print("ship mode failed: {}".format(error))

    def update_buttons(self, now):
        for index, button in enumerate(self.buttons):
            if not button.update(now, self.BUTTON_DEBOUNCE_MS):
                continue

            if self.power_state is None and self.view == "status":
                self.ui.draw_button(index, button.pressed)
            print("{} {}".format(button.name, "down" if button.pressed else "up"))
            self.link.send(BUTTON_EVENT, bytes((index, int(button.pressed))))

            if index == 4:
                if button.pressed:
                    self.begin_power_hold(now)
                elif self.power_state is not None and self.power_state != "powering_off":
                    self.cancel_power_hold(now)
                continue
            if self.power_state is not None or not button.pressed:
                continue

            if index == 0:
                self.open_view("menu" if self.view != "menu" else "status", now)
                continue

            if self.view == "menu":
                if index == 1:
                    self.menu_index = (self.menu_index - 1) % len(self.menu_items)
                elif index == 3:
                    self.menu_index = (self.menu_index + 1) % len(self.menu_items)
                elif index == 2:
                    target = ("ir", "subghz", "status")[self.menu_index]
                    self.open_view(target, now)
                    continue
                self.ui.draw_menu(self.menu_items, self.menu_index)
                continue

            if self.view == "ir":
                if index == 1:
                    self.ir_index = (self.ir_index - 1) % len(self.ir_entries)
                elif index == 3:
                    self.ir_index = (self.ir_index + 1) % len(self.ir_entries)
                elif index == 2:
                    self.activate_ir_entry()
                self.draw_current_view(now)
                continue

            if self.view == "subghz":
                if index == 1:
                    self.sub_index = (self.sub_index - 1) % (len(self.sub_files) + 1)
                    self.load_sub_summary()
                elif index == 3:
                    self.sub_index = (self.sub_index + 1) % (len(self.sub_files) + 1)
                    self.load_sub_summary()
                elif index == 2 and self.sub_pending_index is None:
                    self.activate_sub_entry()
                self.draw_current_view(now)
                continue

            if index == 1:
                self.play_tone()
            elif index == 2:
                self.effects.next_mode()
                self.ui.draw_rgb(self.leds_enabled, self.effects.mode)
                print("RGB effect {}: {}".format(self.effects.mode, self.effects.mode_name))
            elif index == 3:
                self.set_leds_enabled(not self.leds_enabled)

    def update_i2c(self, now):
        if self.power_state is not None:
            return
        if time.ticks_diff(now, self.next_scan_at) >= 0:
            try:
                self.i2c.scan()
            except OSError:
                self.i2c.addresses = ()
            self.next_scan_at = time.ticks_add(now, self.I2C_SCAN_MS)

        if time.ticks_diff(now, self.next_accel_at) >= 0:
            try:
                acceleration = self.i2c.accelerometer()
            except OSError:
                acceleration = None
            if acceleration is not None:
                self.last_acceleration = acceleration
            if self.view == "status":
                self.ui.draw_accelerometer(acceleration)
            self.next_accel_at = time.ticks_add(now, self.ACCEL_UPDATE_MS)

        if time.ticks_diff(now, self.next_status_at) >= 0:
            try:
                rtc = self.i2c.rtc()
            except OSError:
                rtc = None
            if self.view == "status":
                self.ui.draw_rtc(rtc)

            try:
                charger = self.i2c.charger()
            except OSError:
                charger = None
            if charger is not None:
                self.last_battery_mv = charger["battery_mv"]
            try:
                auxiliary = self.i2c.auxiliary()
            except OSError:
                auxiliary = None
            if self.view == "status":
                self.ui.draw_power(charger, self.i2c.healthy_count, auxiliary)
            self.next_status_at = time.ticks_add(now, self.STATUS_UPDATE_MS)

    def handle_link_packet(self, packet):
        message_type, sequence, flags, payload = packet
        if message_type == PING:
            self.link.send(PONG, payload)
        elif message_type == POWER_READY:
            self.power_ready = True
        elif message_type == MAIN_STATUS and len(payload) >= 5:
            self.main_radio_mask = payload[4] & 0x03
            if len(payload) >= 17:
                self.main_radio_rssi = struct.unpack("<bb", payload[15:17])
            if self.view == "subghz" and self.power_state is None:
                self.draw_current_view()
        elif message_type == RADIO_SET_BANDS and len(payload) == 2:
            band1, band2 = payload
            try:
                result = self.i2c.set_radio_bands(band1, band2)
                status = 0
                output = result["output"]
                configuration = result["configuration"]
                self.sub_notice = "FILTER B{}/{} PCA {:02X}/{:02X}".format(
                    band1, band2, output, configuration
                )
            except (OSError, ValueError) as error:
                status = 1
                output = configuration = 0xFF
                self.sub_notice = "ERR RF FILTER {}".format(error)
            self.link.send(
                RADIO_BAND_RESULT,
                bytes((status, band1, band2, output, configuration)),
            )
            if self.view == "subghz" and self.power_state is None:
                self.draw_current_view()
        elif message_type == RADIO_CONFIG_RESULT and len(payload) == 8:
            status, radio_mask, preset, result_flags, frequency = struct.unpack(
                "<BBBBI", payload
            )
            if status == 0:
                self.sub_armed_index = self.sub_pending_index
                self.sub_active_preset = preset
                self.sub_active_frequency = frequency
                self.sub_active_custom = self.sub_pending_custom
                self.last_sub_raw = None
                self.last_sub_decoded = None
                self.sub_notice = "ARMED R{:X} {:.3f}MHz".format(
                    radio_mask,
                    frequency / 1_000_000,
                )
            else:
                self.sub_notice = "ERR RADIO CONFIG {}".format(status)
            self.sub_pending_index = None
            self.sub_pending_custom = b""
            if self.view == "subghz" and self.power_state is None:
                self.draw_current_view()
        elif message_type == RADIO_DECODED and len(payload) == 10:
            radio, protocol, bits, repeats, key, te_us = struct.unpack(
                "<BBBBIH", payload
            )
            if protocol == 1:
                self.last_sub_decoded = (
                    "Princeton",
                    radio + 1,
                    key,
                    bits,
                    te_us,
                    repeats,
                )
                self.sub_notice = "R{} PRINC {:06X} {}us x{}".format(
                    radio + 1, key, te_us, repeats
                )
                if self.view == "subghz" and self.power_state is None:
                    self.draw_current_view()
        elif message_type == RADIO_RAW_CAPTURE and len(payload) >= 6:
            capture_id, radio, chunk_index, capture_flags, count = struct.unpack(
                "<HBBBB", payload[:6]
            )
            if len(payload) != 6 + count * 4:
                return
            if capture_id != self.sub_capture_id:
                self.sub_capture_id = capture_id
                self.sub_capture_radio = radio
                self.sub_capture_parts = []
                self.sub_capture_next_chunk = 0
            if chunk_index != self.sub_capture_next_chunk:
                self.sub_capture_parts = []
                self.sub_capture_next_chunk = 0
                self.sub_notice = "ERR RAW CHUNK ORDER"
                return
            for offset in range(6, len(payload), 4):
                self.sub_capture_parts.append(struct.unpack("<i", payload[offset : offset + 4])[0])
            self.sub_capture_next_chunk += 1
            if capture_flags & 1:
                self.last_sub_raw = tuple(self.sub_capture_parts)
                self.last_sub_truncated = bool(capture_flags & 2)
                self.sub_capture_parts = []
                self.sub_capture_next_chunk = 0
                self.sub_notice = "CAPTURE {} EDGES{}".format(
                    len(self.last_sub_raw),
                    " TRUNC" if self.last_sub_truncated else "",
                )
                if self.view == "subghz" and self.power_state is None:
                    self.draw_current_view()
        elif message_type == RADIO_PACKET and len(payload) >= 4:
            radio, rssi, lqi, packet_flags = struct.unpack("<BbBB", payload[:4])
            self.radio_packet_count = (self.radio_packet_count + 1) & 0xFFFF
            self.last_radio_packet = (
                self.radio_packet_count,
                radio + 1,
                len(payload) - 4,
                rssi,
                "CRC OK" if packet_flags & 1 else "CRC BAD",
            )
            if self.view == "subghz" and self.power_state is None:
                self.draw_current_view()
            print(
                "radio{} RX {} bytes RSSI={} dBm LQI={} CRC={}".format(
                    radio + 1,
                    len(payload) - 4,
                    rssi,
                    lqi,
                    bool(packet_flags & 1),
                )
            )
        elif message_type == IR_NEC_COMMAND and len(payload) == 3:
            address, command = struct.unpack("<HB", payload)
            self.ir_receiver.pause()
            try:
                self.ir_transmitter.send_nec(address, command)
            finally:
                time.sleep_ms(20)
                self.ir_receiver.resume()
                self.ir_activity = self.ir_receiver.activity
            print("IR TX NEC address=0x{:04x} command=0x{:02x}".format(address, command))
        elif message_type == TONE_COMMAND and len(payload) == 5:
            frequency, duration_ms, volume_percent = struct.unpack("<HHB", payload)
            self.play_tone(frequency, duration_ms, min(volume_percent, 10) / 100.0)

    def update_link(self, now):
        for packet in self.link.poll():
            self.handle_link_packet(packet)

        if time.ticks_diff(now, self.next_link_status_at) < 0:
            return

        button_mask = 0
        for index, button in enumerate(self.buttons):
            if button.pressed:
                button_mask |= 1 << index
        x, y, z = self.last_acceleration
        payload = struct.pack(
            "<IBhhhHHH",
            self.uptime_ms(now),
            button_mask,
            x,
            y,
            z,
            self.last_battery_mv,
            self.ir_receiver.activity,
            self.mic_level,
        )
        self.link.send(DISPLAY_STATUS, payload)
        self.link.send(HEARTBEAT, struct.pack("<I", self.uptime_ms(now)))
        self.link.send_hello()
        self.next_link_status_at = time.ticks_add(now, self.LINK_STATUS_MS)

    def update_services(self, now):
        decoded = self.ir_receiver.poll()
        if self.ir_receiver.activity != self.ir_activity:
            self.ir_activity = self.ir_receiver.activity
            self.ir_active_until = time.ticks_add(now, 250)
        for address, command in decoded:
            protocol = self.ir_receiver.last_protocol or "NEC?"
            self.last_ir_nec = (address, command)
            self.last_ir_protocol = protocol
            self.ir_decoded_since_capture = True
            print(
                "IR RX {} address=0x{:04x} command=0x{:04x}".format(
                    protocol, address, command
                )
            )
            protocol_id = 1 if protocol == "NEC" else 2
            self.link.send(
                IR_RECEIVED,
                struct.pack("<BHH", protocol_id, address, command),
            )

        raw = self.ir_receiver.take_raw()
        if raw is not None:
            self.last_ir_raw = raw
            if self.ir_decoded_since_capture:
                self.last_ir_capture_parsed = (
                    self.last_ir_protocol,
                    self.last_ir_nec[0],
                    self.last_ir_nec[1],
                )
                self.ir_notice = "DECODED {} // {} EDGES".format(
                    self.last_ir_protocol,
                    len(raw),
                )
            elif self.ir_receiver.last_failure == "NEC REPEAT":
                self.last_ir_capture_parsed = None
                self.ir_notice = "NEC REPEAT // NO NEW CODE"
            else:
                self.last_ir_capture_parsed = None
                reason = self.ir_receiver.last_failure or "NOT NEC"
                self.ir_notice = "DECODE FAIL: {}".format(reason)
            self.ir_decoded_since_capture = False
        if (decoded or raw is not None) and self.view == "ir" and self.power_state is None:
            self.draw_current_view(now)

        if time.ticks_diff(now, self.next_service_at) < 0:
            return
        if self.microphone is not None:
            self.mic_level = self.microphone.read_level()
        ir_active = time.ticks_diff(self.ir_active_until, now) > 0
        if self.power_state is None and self.view == "status":
            self.ui.draw_services(
                self.link.connected,
                self.main_radio_mask,
                ir_active,
                self.mic_level,
            )
        self.next_service_at = time.ticks_add(now, self.SERVICE_UPDATE_MS)

    def update_effects(self, now):
        if self.power_state in ("preparing", "powering_off"):
            return
        if self.leds_enabled:
            self.effects.update(now)

    def run(self):
        print("OpenKeiko hardware dashboard started")
        try:
            while True:
                feed_watchdog(self.watchdog)
                now = time.ticks_ms()
                self.update_buttons(now)
                self.update_power_control(now)
                self.update_i2c(now)
                self.update_link(now)
                self.update_services(now)
                self.update_effects(now)
                time.sleep_ms(1)
        finally:
            self.leds.off()
            if self.speaker is not None:
                self.speaker.deinit()
            if self.microphone is not None:
                self.microphone.deinit()
            self.ir_receiver.deinit()
            self.ir_transmitter.deinit()
            self.link.deinit()
            self.tft.deinit()


DisplayController().run()
