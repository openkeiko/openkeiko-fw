"""Receive-only CC1101 support for both OpenKeiko FW1 radios."""

from machine import Pin, SPI
import rp2
import struct
import time


# Candidate stock-style 433.92 MHz, 2-FSK receive profile. Values derived from
# the CC1101 equations and the v92 UI defaults: 99.976 kBaud, 47.607 kHz
# deviation, 812.5 kHz RX bandwidth, and 199.951 kHz channel spacing.
PRESET_PACKET_2FSK = 0
PRESET_OOK_270 = 1
PRESET_OOK_650 = 2
PRESET_2FSK_DEV_238 = 3
PRESET_2FSK_DEV_12 = 4
PRESET_2FSK_DEV_476 = 5
PRESET_CUSTOM = 255


RX_433_92_2FSK = (
    (0x00, 0x06),  # IOCFG2: packet sync/end indication
    (0x02, 0x06),  # IOCFG0: packet sync/end indication
    (0x03, 0x47),  # FIFOTHR
    (0x04, 0xD3),  # SYNC1
    (0x05, 0x91),  # SYNC0
    (0x06, 0xFF),  # PKTLEN
    (0x07, 0x04),  # PKTCTRL1: append RSSI/LQI status
    (0x08, 0x05),  # PKTCTRL0: variable length, CRC enabled
    (0x09, 0x00),  # ADDR
    (0x0A, 0x00),  # CHANNR
    (0x0B, 0x06),  # FSCTRL1
    (0x0C, 0x00),  # FSCTRL0
    (0x0D, 0x10),  # FREQ2
    (0x0E, 0xB0),  # FREQ1
    (0x0F, 0x71),  # FREQ0: 433.919830 MHz at 26 MHz crystal
    (0x10, 0x0B),  # MDMCFG4: 812.5 kHz BW, data-rate exponent 11
    (0x11, 0xF8),  # MDMCFG3: 99.9756 kBaud
    (0x12, 0x03),  # MDMCFG2: 2-FSK, 30/32 sync
    (0x13, 0x22),  # MDMCFG1: four-byte preamble, spacing exponent 2
    (0x14, 0xF8),  # MDMCFG0: 199.951 kHz spacing
    (0x15, 0x47),  # DEVIATN: 47.607 kHz
    (0x17, 0x30),  # MCSM1: CCA, RX and TX completion both return to IDLE
    (0x18, 0x18),  # MCSM0: calibrate when leaving IDLE
    (0x19, 0x16),  # FOCCFG
    (0x1A, 0x6C),  # BSCFG
)

# Receive-side copies of Flipper's asynchronous CC1101 presets. PATABLE bytes
# are intentionally omitted and are always replaced with eight zero bytes.
_FLIPPER_OOK_COMMON = (
    (0x02, 0x0D), (0x08, 0x32), (0x0B, 0x06),
    (0x14, 0x00), (0x13, 0x00), (0x12, 0x30), (0x11, 0x32),
    (0x18, 0x18), (0x19, 0x18), (0x20, 0xFB),
    (0x22, 0x11), (0x21, 0xB6),
)
FLIPPER_OOK_270 = _FLIPPER_OOK_COMMON + (
    (0x03, 0x47), (0x10, 0x67), (0x1D, 0x40), (0x1C, 0x00), (0x1B, 0x03),
)
FLIPPER_OOK_650 = _FLIPPER_OOK_COMMON + (
    (0x03, 0x07), (0x10, 0x17), (0x1D, 0x91), (0x1C, 0x00), (0x1B, 0x07),
)
_FLIPPER_2FSK_COMMON = (
    (0x02, 0x0D), (0x0B, 0x06), (0x08, 0x32), (0x07, 0x04),
    (0x14, 0x00), (0x13, 0x02), (0x12, 0x04), (0x11, 0x83), (0x10, 0x67),
    (0x18, 0x18), (0x19, 0x16),
    (0x1D, 0x91), (0x1C, 0x00), (0x1B, 0x07), (0x20, 0xFB),
    (0x22, 0x10), (0x21, 0x56),
)
FLIPPER_2FSK_DEV_238 = _FLIPPER_2FSK_COMMON + ((0x15, 0x04),)
FLIPPER_2FSK_DEV_12 = _FLIPPER_2FSK_COMMON + ((0x15, 0x30),)
FLIPPER_2FSK_DEV_476 = _FLIPPER_2FSK_COMMON + ((0x15, 0x47),)
FLIPPER_RX_PRESETS = {
    PRESET_OOK_270: FLIPPER_OOK_270,
    PRESET_OOK_650: FLIPPER_OOK_650,
    PRESET_2FSK_DEV_238: FLIPPER_2FSK_DEV_238,
    PRESET_2FSK_DEV_12: FLIPPER_2FSK_DEV_12,
    PRESET_2FSK_DEV_476: FLIPPER_2FSK_DEV_476,
}


@rp2.asm_pio(
    in_shiftdir=rp2.PIO.SHIFT_LEFT,
    autopush=True,
    push_thresh=32,
)
def _sample_gdo():
    in_(pins, 1)


class AsyncCapture:
    SAMPLE_HZ = 200_000
    SAMPLE_US = 5
    # 61.44 ms covers a complete 24-bit Princeton frame plus its sync gap.
    DMA_WORDS = 384
    FRAME_GAP_SAMPLES = 6_000
    MAX_TIMINGS = 512

    def __init__(self, pin, state_machine):
        self.pin = pin
        self.state_machine = state_machine
        self.sm = rp2.StateMachine(
            state_machine,
            _sample_gdo,
            freq=self.SAMPLE_HZ,
            in_base=pin,
        )
        self.dma = rp2.DMA()
        self.buffer = bytearray(self.DMA_WORDS * 4)
        sm_index = state_machine & 3
        pio_index = state_machine >> 2
        pio_base = 0x50200000 + pio_index * 0x00100000
        self.rx_fifo = pio_base + 0x20 + sm_index * 4
        self.control = self.dma.pack_ctrl(
            inc_read=False,
            inc_write=True,
            treq_sel=4 + sm_index + pio_index * 8,
            size=2,
        )
        self._capture = []
        self._ready = []
        self._level = None
        self._run = 0
        self.truncated = False
        self.enabled = False

    def _arm_dma(self):
        self.dma.config(
            read=self.rx_fifo,
            write=self.buffer,
            count=self.DMA_WORDS,
            ctrl=self.control,
            trigger=True,
        )

    def start(self):
        self.stop()
        while self.sm.rx_fifo():
            self.sm.get()
        self._capture = []
        self._ready = []
        self._level = None
        self._run = 0
        self.truncated = False
        self.enabled = True
        self.sm.active(1)
        self._arm_dma()

    def stop(self):
        if self.dma.active():
            self.dma.active(False)
        self.sm.active(0)
        self.enabled = False
        self._capture = []
        self._ready = []

    def close(self):
        self.stop()
        self.dma.close()

    def _finish_frame(self):
        if len(self._capture) >= 4:
            self._ready.append((self._capture, self.truncated))
        self._capture = []
        self.truncated = False

    def _finish_run(self, level, samples):
        if samples <= 0:
            return
        if samples >= self.FRAME_GAP_SAMPLES:
            if self._capture:
                self._finish_frame()
            return
        duration = samples * self.SAMPLE_US
        if not self._capture:
            if level and duration >= 20:
                self._capture.append(duration)
            return
        if len(self._capture) < self.MAX_TIMINGS:
            self._capture.append(duration if level else -duration)
        else:
            self.truncated = True

    def _consume_level(self, level):
        if self._level is None:
            self._level = level
            self._run = 1
        elif level == self._level:
            self._run += 1
        else:
            self._finish_run(self._level, self._run)
            self._level = level
            self._run = 1

    def poll(self):
        if not self.enabled:
            return None
        if not self.dma.active():
            for offset in range(0, len(self.buffer), 4):
                word = struct.unpack_from("<I", self.buffer, offset)[0]
                for bit in range(31, -1, -1):
                    self._consume_level((word >> bit) & 1)
            self._arm_dma()

        if self._run >= self.FRAME_GAP_SAMPLES and self._capture:
            self._finish_frame()
        if self._ready:
            return self._ready.pop(0)
        return None


class CC1101:
    READ = 0x80
    BURST = 0x40

    PARTNUM = 0x30
    VERSION = 0x31
    RSSI = 0x34
    MARCSTATE = 0x35
    PKTSTATUS = 0x38
    RXBYTES = 0x3B
    PATABLE = 0x3E
    FIFO = 0x3F

    SRES = 0x30
    SRX = 0x34
    SIDLE = 0x36
    SFRX = 0x3A
    SNOP = 0x3D
    _SAFE_STROBES = (SRES, SRX, SIDLE, SFRX, SNOP)

    STATE_IDLE = 0x01
    STATE_RX = 0x0D

    def __init__(self, spi, miso, chip_select, gdo0, gdo2, name, capture_sm):
        self.spi = spi
        self.miso = miso
        self.cs = Pin(chip_select, Pin.OUT, value=1)
        self.gdo0 = Pin(gdo0, Pin.IN)
        self.gdo2 = Pin(gdo2, Pin.IN)
        self.name = name
        self.last_probe = None
        self.receive_enabled = False
        self.receive_mode = "idle"
        self.frequency_hz = 0
        self.capture = AsyncCapture(self.gdo0, capture_sm)
        self._packet_length = None
        self._packet_data = bytearray()

    def _select(self, timeout_us=2_000):
        self.cs.off()
        started = time.ticks_us()
        while self.miso.value():
            if time.ticks_diff(time.ticks_us(), started) >= timeout_us:
                self.cs.on()
                raise OSError("{} SPI not ready".format(self.name))

    def _transfer(self, tx):
        rx = bytearray(len(tx))
        self._select()
        try:
            self.spi.write_readinto(tx, rx)
        finally:
            self.cs.on()
        return rx

    def _read(self, address, status=False):
        header = address | self.READ | (self.BURST if status else 0)
        return self._transfer(bytes((header, 0x00)))[1]

    def _read_burst(self, address, length):
        if length <= 0:
            return b""
        tx = bytes((address | self.READ | self.BURST,)) + bytes(length)
        return bytes(self._transfer(tx)[1:])

    def _write_register(self, address, value):
        if not 0 <= address <= 0x2E:
            raise ValueError("configuration register required")
        self._transfer(bytes((address, value & 0xFF)))

    def _write_burst(self, address, data):
        # The only non-configuration burst write exposed by this RX-only class
        # is an all-zero PATABLE, which makes accidental TX produce no RF power.
        if address == self.PATABLE:
            if len(data) != 8 or any(data):
                raise ValueError("RX-only PATABLE must contain eight zeroes")
        elif not 0 <= address <= 0x2E:
            raise ValueError("unsafe burst-write address")
        self._transfer(bytes((address | self.BURST,)) + bytes(data))

    def _strobe(self, command):
        if command not in self._SAFE_STROBES:
            raise ValueError("unsafe CC1101 command strobe")
        return self._transfer(bytes((command,)))[0]

    def read_config(self, address):
        return self._read(address, status=False)

    def read_status(self, address):
        return self._read(address, status=True)

    @staticmethod
    def rssi_dbm(raw):
        signed = raw - 256 if raw >= 128 else raw
        return signed // 2 - 74

    def sample_rssi(self, count=16, interval_ms=2):
        if not self.receive_enabled:
            raise OSError("{} is not in receive mode".format(self.name))
        count = max(1, min(128, int(count)))
        values = []
        for _ in range(count):
            values.append(self.rssi_dbm(self.read_status(self.RSSI)))
            if interval_ms:
                time.sleep_ms(interval_ms)
        return {
            "samples": count,
            "average_dbm": sum(values) // count,
            "minimum_dbm": min(values),
            "maximum_dbm": max(values),
        }

    def reset(self):
        self.cs.on()
        time.sleep_us(45)
        self.cs.off()
        time.sleep_us(12)
        self.cs.on()
        time.sleep_us(45)
        self._strobe(self.SRES)
        self.capture.stop()
        self.receive_enabled = False
        self.receive_mode = "idle"
        self._packet_length = None
        self._packet_data = bytearray()

    def idle(self):
        self._strobe(self.SIDLE)
        started = time.ticks_ms()
        while (self.read_status(self.MARCSTATE) & 0x1F) != self.STATE_IDLE:
            if time.ticks_diff(time.ticks_ms(), started) >= 20:
                raise OSError("{} failed to enter IDLE".format(self.name))
        self.capture.stop()
        self.receive_enabled = False
        self.receive_mode = "idle"

    def flush_receive(self):
        self.idle()
        self._strobe(self.SFRX)

    def apply_receive_profile(self, profile=RX_433_92_2FSK):
        self.idle()
        self._write_burst(self.PATABLE, bytes(8))
        for address, value in profile:
            self._write_register(address, value)
        for address, expected in profile:
            actual = self.read_config(address)
            if actual != expected:
                raise OSError(
                    "{} register 0x{:02x}: 0x{:02x} != 0x{:02x}".format(
                        self.name, address, actual, expected
                    )
                )

    @staticmethod
    def band_for_frequency(frequency_hz):
        if 300_000_000 <= frequency_hz <= 348_000_000:
            return 1
        if 387_000_000 <= frequency_hz <= 464_000_000:
            return 2
        if 779_000_000 <= frequency_hz <= 928_000_000:
            return 3
        raise ValueError("unsupported CC1101 receive frequency")

    @classmethod
    def frequency_allowed(cls, frequency_hz):
        try:
            cls.band_for_frequency(frequency_hz)
            return True
        except ValueError:
            return False

    def set_frequency(self, frequency_hz):
        frequency_hz = int(frequency_hz)
        if not self.frequency_allowed(frequency_hz):
            raise ValueError("unsupported CC1101 receive frequency")
        word = ((frequency_hz << 16) + 13_000_000) // 26_000_000
        self._write_register(0x0D, (word >> 16) & 0xFF)
        self._write_register(0x0E, (word >> 8) & 0xFF)
        self._write_register(0x0F, word & 0xFF)
        self.frequency_hz = (word * 26_000_000) >> 16
        return self.frequency_hz

    def apply_async_profile(self, profile, frequency_hz):
        self.idle()
        self._write_burst(self.PATABLE, bytes(8))
        for address, value in profile:
            self._write_register(address, value)
        actual_frequency = self.set_frequency(frequency_hz)
        for address, expected in profile:
            actual = self.read_config(address)
            if actual != expected:
                raise OSError(
                    "{} async register 0x{:02x} verify failed".format(
                        self.name, address
                    )
                )
        return actual_frequency

    def start_receive(self, mode="packet"):
        self.flush_receive()
        self._strobe(self.SRX)
        started = time.ticks_ms()
        while (self.read_status(self.MARCSTATE) & 0x1F) != self.STATE_RX:
            if time.ticks_diff(time.ticks_ms(), started) >= 30:
                raise OSError("{} failed to enter RX".format(self.name))
        self.receive_enabled = True
        self.receive_mode = mode
        if mode == "async":
            self.capture.start()

    def initialize_receive(self, profile=RX_433_92_2FSK):
        self.reset()
        identity = self.probe()
        if not identity["present"]:
            raise OSError("{} identity mismatch".format(self.name))
        self.apply_receive_profile(profile)
        self.frequency_hz = 433_919_830
        self.start_receive("packet")
        return self.probe()

    def initialize_async_receive(self, profile, frequency_hz):
        self.reset()
        identity = self.probe()
        if not identity["present"]:
            raise OSError("{} identity mismatch".format(self.name))
        actual_frequency = self.apply_async_profile(profile, frequency_hz)
        self.start_receive("async")
        return self.probe(), actual_frequency

    def probe(self):
        try:
            part = self.read_status(self.PARTNUM)
            version = self.read_status(self.VERSION)
            marcstate = self.read_status(self.MARCSTATE) & 0x1F
            pktstatus = self.read_status(self.PKTSTATUS)
            rxbytes = self.read_status(self.RXBYTES)
            raw_rssi = self.read_status(self.RSSI)
            present = part == 0x00 and version == 0x14
        except OSError:
            part = version = marcstate = pktstatus = rxbytes = raw_rssi = 0xFF
            present = False

        self.last_probe = {
            "present": present,
            "part": part,
            "version": version,
            "marcstate": marcstate,
            "pktstatus": pktstatus,
            "rxbytes": rxbytes & 0x7F,
            "rx_overflow": bool(rxbytes & 0x80),
            "rssi_dbm": self.rssi_dbm(raw_rssi) if present else None,
            "gdo0": self.gdo0.value(),
            "gdo2": self.gdo2.value(),
            "receive": self.receive_enabled,
            "mode": self.receive_mode,
            "frequency_hz": self.frequency_hz,
        }
        return self.last_probe

    def poll_packet(self):
        if self.receive_mode != "packet":
            return None
        status = self.read_status(self.RXBYTES)
        if status & 0x80:
            self._packet_length = None
            self._packet_data = bytearray()
            self.start_receive()
            return None

        available = status & 0x7F
        if self._packet_length is None:
            if available == 0:
                return None
            self._packet_length = self._read_burst(self.FIFO, 1)[0]
            self._packet_data = bytearray()
            available -= 1

        required = self._packet_length + 2 - len(self._packet_data)
        if available and required > 0:
            chunk = self._read_burst(self.FIFO, min(available, required))
            self._packet_data.extend(chunk)

        if len(self._packet_data) < self._packet_length + 2:
            return None

        payload = bytes(self._packet_data[: self._packet_length])
        raw_rssi = self._packet_data[self._packet_length]
        lqi_crc = self._packet_data[self._packet_length + 1]
        signed_rssi = raw_rssi - 256 if raw_rssi >= 128 else raw_rssi
        packet = {
            "data": payload,
            "rssi_dbm": signed_rssi // 2 - 74,
            "lqi": lqi_crc & 0x7F,
            "crc_ok": bool(lqi_crc & 0x80),
        }
        self._packet_length = None
        self._packet_data = bytearray()
        return packet

    def recover_overflow(self):
        if self.receive_mode != "packet":
            return False
        if self.read_status(self.RXBYTES) & 0x80:
            self._packet_length = None
            self._packet_data = bytearray()
            self.start_receive()
            return True
        return False

    def deinit(self):
        try:
            self.idle()
        except OSError:
            pass
        self.capture.close()
        self.cs.on()


class FW1Radios:
    def __init__(self, baudrate=1_000_000):
        # Select lines are made inactive before assigning the shared SPI pins.
        self.cs1 = Pin(18, Pin.OUT, value=1)
        self.cs2 = Pin(5, Pin.OUT, value=1)
        self.miso = Pin(4, Pin.IN)
        self.spi = SPI(
            0,
            baudrate=baudrate,
            polarity=0,
            phase=0,
            bits=8,
            firstbit=SPI.MSB,
            sck=Pin(6),
            mosi=Pin(7),
            miso=self.miso,
        )
        self.radio1 = CC1101(self.spi, self.miso, 18, 21, 19, "radio1", 0)
        self.radio2 = CC1101(self.spi, self.miso, 5, 20, 22, "radio2", 1)
        self.radios = (self.radio1, self.radio2)
        self.active_preset = PRESET_PACKET_2FSK
        self.active_frequency_hz = 433_919_830

    @staticmethod
    def custom_receive_profile(data):
        profile = []
        terminator = -1
        for offset in range(0, len(data) - 1, 2):
            address = data[offset]
            value = data[offset + 1]
            if address == 0 and value == 0:
                terminator = offset
                break
            if address > 0x2E:
                raise ValueError("invalid custom CC1101 register")
            profile.append((address, value))
        if terminator < 0 or len(data) - terminator - 2 != 8:
            raise ValueError("invalid custom CC1101 preset")
        return tuple(profile)

    def configure_receive(self, radio_mask, preset, frequency_hz, custom=b""):
        if not radio_mask & 0x03:
            raise ValueError("empty radio mask")
        if preset == PRESET_PACKET_2FSK:
            if frequency_hz not in (0, 433_920_000):
                raise ValueError("packet profile is fixed at 433.92 MHz")
            profile = None
        elif preset == PRESET_CUSTOM:
            profile = self.custom_receive_profile(custom)
        else:
            try:
                profile = FLIPPER_RX_PRESETS[preset]
            except KeyError:
                raise ValueError("unsupported Flipper preset")

        configured_mask = 0
        actual_frequency = 433_919_830
        for index, radio in enumerate(self.radios):
            if not radio_mask & (1 << index):
                continue
            if profile is None:
                radio.initialize_receive()
                actual_frequency = radio.frequency_hz
            else:
                _, actual_frequency = radio.initialize_async_receive(
                    profile, frequency_hz
                )
            configured_mask |= 1 << index
        self.active_preset = preset
        self.active_frequency_hz = actual_frequency
        return configured_mask, actual_frequency

    def sample_rssi(self, count=16, interval_ms=2):
        return tuple(
            radio.sample_rssi(count=count, interval_ms=interval_ms)
            for radio in self.radios
        )

    def radio_bands(self):
        return tuple(
            CC1101.band_for_frequency(
                radio.frequency_hz or self.active_frequency_hz
            )
            for radio in self.radios
        )

    def poll_raw_captures(self):
        captures = []
        for index, radio in enumerate(self.radios):
            result = radio.capture.poll()
            if result is not None:
                timings, truncated = result
                captures.append((index, timings, truncated))
        return captures

    def probe(self):
        return tuple(radio.probe() for radio in self.radios)

    def initialize_receive(self):
        status = []
        for radio in self.radios:
            try:
                status.append(radio.initialize_receive())
            except OSError as error:
                print("{} RX disabled: {}".format(radio.name, error))
                status.append(radio.probe())
        return tuple(status)

    def poll_packets(self):
        packets = []
        for index, radio in enumerate(self.radios):
            if not radio.receive_enabled:
                continue
            try:
                packet = radio.poll_packet()
            except OSError as error:
                radio.receive_enabled = False
                print("{} RX poll failed: {}".format(radio.name, error))
                continue
            if packet is not None:
                packets.append((index, packet))
        return packets

    def prepare_shutdown(self):
        for radio in self.radios:
            try:
                radio.idle()
            except OSError as error:
                print("{} shutdown idle failed: {}".format(radio.name, error))

    def resume_receive(self):
        return self.initialize_receive()

    def maintain_receive(self):
        for radio in self.radios:
            if not radio.last_probe or not radio.last_probe["present"]:
                continue
            try:
                if radio.recover_overflow():
                    print("{} RX overflow recovered".format(radio.name))
            except OSError as error:
                radio.receive_enabled = False
                print("{} RX maintenance failed: {}".format(radio.name, error))

    def deinit(self):
        for radio in self.radios:
            radio.deinit()
        self.spi.deinit()
