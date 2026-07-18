"""Drivers for the OpenKeiko FW1 display-side I2C devices."""

from machine import I2C, Pin
import struct


class FW1I2C:
    LIS3DH = 0x19
    AUX = 0x21
    BQ25892 = 0x6B
    MCP7940 = 0x6F

    EXPECTED = (LIS3DH, AUX, BQ25892, MCP7940)

    # PCA9555 port-1 bits recovered from display v67 command 0x07.
    RADIO1_BAND_LOW = 1 << 1
    RADIO2_BAND_LOW = 1 << 2
    RADIO1_BAND_HIGH = 1 << 3
    RADIO2_BAND_HIGH = 1 << 4
    RADIO_BAND_MASK = 0x1E

    # PCA9555 port-0 buffer directions recovered from display v67 and main
    # v92. A one selects FPGA/RP2040-to-header direction in stock firmware.
    HEADER_UART_RTS = 1 << 1
    HEADER_UART_RX = 1 << 2
    HEADER_UART_TX = 1 << 3
    HEADER_SPI_MOSI = 1 << 4
    HEADER_SPI_MISO = 1 << 5
    HEADER_SPI_CS = 1 << 6
    HEADER_SPI_SCLK = 1 << 7
    HEADER_OUTPUT_TEST_MASK = (
        HEADER_UART_RTS
        | HEADER_UART_TX
        | HEADER_SPI_MOSI
        | HEADER_SPI_CS
        | HEADER_SPI_SCLK
    )

    def __init__(self, frequency=400_000):
        self.bus = I2C(
            1,
            sda=Pin(26),
            scl=Pin(27),
            freq=frequency,
        )
        self.addresses = ()
        self.initialize()

    def initialize(self):
        self.scan()

        if self.LIS3DH in self.addresses:
            # 100 Hz, XYZ enabled; block update, high resolution, +/-2 g.
            self.bus.writeto_mem(self.LIS3DH, 0x20, b"\x57")
            self.bus.writeto_mem(self.LIS3DH, 0x23, b"\x88")

        if self.BQ25892 in self.addresses:
            # Match stock behavior by enabling continuous ADC conversion while
            # preserving every charger-control bit already programmed.
            control = self.bus.readfrom_mem(self.BQ25892, 0x02, 1)[0]
            self.bus.writeto_mem(self.BQ25892, 0x02, bytes((control | 0xC0,)))

    def scan(self):
        self.addresses = tuple(self.bus.scan())
        return self.addresses

    @property
    def healthy_count(self):
        return sum(address in self.addresses for address in self.EXPECTED)

    def accelerometer(self):
        data = self.bus.readfrom_mem(self.LIS3DH, 0x28 | 0x80, 6)
        x, y, z = struct.unpack("<hhh", data)
        # At +/-2 g in high-resolution mode each right-shifted LSB is 1 mg.
        return x >> 4, y >> 4, z >> 4

    @staticmethod
    def _bcd(value):
        return ((value >> 4) * 10) + (value & 0x0F)

    @staticmethod
    def _to_bcd(value):
        return ((value // 10) << 4) | (value % 10)

    def set_rtc(self, year, month, day, weekday, hour, minute, second):
        data = bytes((
            self._to_bcd(second) | 0x80,
            self._to_bcd(minute),
            self._to_bcd(hour),
            0x08 | ((weekday + 1) & 0x07),
            self._to_bcd(day),
            self._to_bcd(month),
            self._to_bcd(year % 100),
        ))
        self.bus.writeto_mem(self.MCP7940, 0x00, data)

    def rtc(self):
        data = self.bus.readfrom_mem(self.MCP7940, 0x00, 7)
        running = bool(data[0] & 0x80)
        second = self._bcd(data[0] & 0x7F)
        minute = self._bcd(data[1] & 0x7F)

        if data[2] & 0x40:
            hour = self._bcd(data[2] & 0x1F)
            if data[2] & 0x20:
                hour = (hour % 12) + 12
        else:
            hour = self._bcd(data[2] & 0x3F)

        day = self._bcd(data[4] & 0x3F)
        month = self._bcd(data[5] & 0x1F)
        year = 2000 + self._bcd(data[6])
        return running, year, month, day, hour, minute, second

    def charger(self):
        status = self.bus.readfrom_mem(self.BQ25892, 0x0B, 1)[0]
        misc = self.bus.readfrom_mem(self.BQ25892, 0x09, 1)[0]
        part = self.bus.readfrom_mem(self.BQ25892, 0x14, 1)[0]
        battery = self.bus.readfrom_mem(self.BQ25892, 0x0E, 1)[0]
        vbus = self.bus.readfrom_mem(self.BQ25892, 0x11, 1)[0]
        current = self.bus.readfrom_mem(self.BQ25892, 0x12, 1)[0]

        source_names = (
            "NONE",
            "USB SDP",
            "USB CDP",
            "USB DCP",
            "MAXCHG",
            "UNKNOWN",
            "OTG",
            "RESERVED",
        )
        charge_names = ("IDLE", "PRECHG", "FAST", "DONE")

        return {
            "part": "BQ25892" if ((part >> 3) & 0x07) == 0 else "BQ2589X",
            "revision": part & 0x03,
            "source": source_names[(status >> 5) & 0x07],
            "charge": charge_names[(status >> 3) & 0x03],
            "power_good": bool(status & 0x04),
            "battery_mv": 2304 + (battery & 0x7F) * 20,
            "vbus_mv": 2600 + (vbus & 0x7F) * 100,
            "vbus_good": bool(vbus & 0x80),
            "charge_ma": (current & 0x7F) * 50,
            "batfet_disabled": bool(misc & 0x20),
        }

    @staticmethod
    def has_external_power(charger):
        return bool(
            charger
            and (
                charger["power_good"]
                or charger["vbus_good"]
                or charger["source"] != "NONE"
            )
        )

    def enter_ship_mode(self):
        charger = self.charger()
        if self.has_external_power(charger):
            raise OSError("external power is present")
        misc = self.bus.readfrom_mem(self.BQ25892, 0x09, 1)[0]
        target = misc | 0x20
        self.bus.writeto_mem(self.BQ25892, 0x09, bytes((target,)))
        return target

    @staticmethod
    def radio_band_for_frequency(frequency_hz):
        frequency_hz = int(frequency_hz)
        if 300_000_000 <= frequency_hz <= 348_000_000:
            return 1
        if 387_000_000 <= frequency_hz <= 464_000_000:
            return 2
        if 779_000_000 <= frequency_hz <= 928_000_000:
            return 3
        raise ValueError("unsupported radio filter frequency")

    @classmethod
    def _radio_band_bits(cls, radio1_band, radio2_band):
        if radio1_band not in (1, 2, 3) or radio2_band not in (1, 2, 3):
            raise ValueError("radio band code must be 1, 2, or 3")
        return (
            ((radio1_band & 1) << 1)
            | ((radio2_band & 1) << 2)
            | ((radio1_band & 2) << 2)
            | ((radio2_band & 2) << 3)
        )

    def set_radio_bands(self, radio1_band, radio2_band):
        """Drive only the four recovered PCA9555 RF filter-select outputs."""
        band_bits = self._radio_band_bits(radio1_band, radio2_band)
        output = self.bus.readfrom_mem(self.AUX, 0x03, 1)[0]
        configuration = self.bus.readfrom_mem(self.AUX, 0x07, 1)[0]
        target_output = (output & ~self.RADIO_BAND_MASK) | band_bits
        target_configuration = configuration & ~self.RADIO_BAND_MASK

        # Program the latch before changing pin direction to avoid glitches.
        self.bus.writeto_mem(self.AUX, 0x03, bytes((target_output,)))
        self.bus.writeto_mem(self.AUX, 0x07, bytes((target_configuration,)))
        actual_output = self.bus.readfrom_mem(self.AUX, 0x03, 1)[0]
        actual_configuration = self.bus.readfrom_mem(self.AUX, 0x07, 1)[0]
        if actual_output != target_output or actual_configuration != target_configuration:
            raise OSError("radio filter-select verification failed")
        return {
            "radio1_band": radio1_band,
            "radio2_band": radio2_band,
            "output": actual_output,
            "configuration": actual_configuration,
        }

    def disable_radio_bands(self):
        """Return all four RF filter-select pins to high impedance."""
        configuration = self.bus.readfrom_mem(self.AUX, 0x07, 1)[0]
        target = configuration | self.RADIO_BAND_MASK
        self.bus.writeto_mem(self.AUX, 0x07, bytes((target,)))
        actual = self.bus.readfrom_mem(self.AUX, 0x07, 1)[0]
        if actual != target:
            raise OSError("radio filter-select disable verification failed")
        return actual

    def enable_header_output_test_paths(self):
        """Enable only the five recovered UART/SPI output-default paths."""
        mask = self.HEADER_OUTPUT_TEST_MASK
        output = self.bus.readfrom_mem(self.AUX, 0x02, 1)[0]
        configuration = self.bus.readfrom_mem(self.AUX, 0x06, 1)[0]
        target_output = output | mask
        target_configuration = configuration & ~mask

        try:
            # Establish high idle levels before directing the buffers outward.
            self.bus.writeto_mem(self.AUX, 0x02, bytes((target_output,)))
            self.bus.writeto_mem(self.AUX, 0x06, bytes((target_configuration,)))
            actual_output = self.bus.readfrom_mem(self.AUX, 0x02, 1)[0]
            actual_configuration = self.bus.readfrom_mem(self.AUX, 0x06, 1)[0]
            if (
                actual_output != target_output
                or actual_configuration != target_configuration
            ):
                raise OSError("header output direction verification failed")
        except Exception:
            # Best-effort rollback keeps tested paths inputs while restoring.
            try:
                current = self.bus.readfrom_mem(self.AUX, 0x06, 1)[0]
                self.bus.writeto_mem(self.AUX, 0x06, bytes((current | mask,)))
                self.bus.writeto_mem(self.AUX, 0x02, bytes((output,)))
                self.bus.writeto_mem(self.AUX, 0x06, bytes((configuration,)))
            except Exception:
                pass
            raise
        return {
            "mask": mask,
            "output": output,
            "configuration": configuration,
        }

    def restore_header_output_test_paths(self, state):
        """Restore a state returned by enable_header_output_test_paths()."""
        mask = self.HEADER_OUTPUT_TEST_MASK
        try:
            state_mask = state["mask"]
            saved_output = int(state["output"])
            saved_configuration = int(state["configuration"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("invalid header output state")
        if (
            state_mask != mask
            or not 0 <= saved_output <= 0xFF
            or not 0 <= saved_configuration <= 0xFF
        ):
            raise ValueError("invalid header output state")

        output = self.bus.readfrom_mem(self.AUX, 0x02, 1)[0]
        configuration = self.bus.readfrom_mem(self.AUX, 0x06, 1)[0]
        # Make the tested paths inputs before restoring their output latches.
        self.bus.writeto_mem(self.AUX, 0x06, bytes((configuration | mask,)))
        restored_output = (output & ~mask) | (saved_output & mask)
        self.bus.writeto_mem(self.AUX, 0x02, bytes((restored_output,)))
        restored_configuration = (
            (configuration & ~mask) | (saved_configuration & mask)
        )
        self.bus.writeto_mem(self.AUX, 0x06, bytes((restored_configuration,)))

        actual_output = self.bus.readfrom_mem(self.AUX, 0x02, 1)[0]
        actual_configuration = self.bus.readfrom_mem(self.AUX, 0x06, 1)[0]
        if (
            actual_output != restored_output
            or actual_configuration != restored_configuration
        ):
            raise OSError("header output direction restoration failed")
        return {
            "output": actual_output,
            "configuration": actual_configuration,
        }

    def auxiliary(self):
        # Read PCA9555-compatible input ports 0 and 1 without changing its
        # output latches or direction registers.
        return self.bus.readfrom_mem(self.AUX, 0x00, 2)
