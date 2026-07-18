"""Bounded volatile iCE40UP5K programming for the OpenKeiko FW1."""

from machine import Pin, SPI, mem32
import hashlib
import time


class FW1FPGAClock:
    """Drive the FPGA clock from clk_sys through RP2040 GPOUT1/GPIO23."""

    PIN = 23
    IO_BANK0_GPIO_CTRL = 0x40014004
    CLOCKS_CLK_GPOUT1_CTRL = 0x4000800C
    CLOCKS_CLK_GPOUT1_DIV = 0x40008010

    GPIO_FUNC_GPCK = 8
    CTRL_ENABLE = 1 << 11
    CTRL_DC50 = 1 << 12
    AUXSRC_CLK_SYS = 6 << 5

    def __init__(self):
        self.pin = Pin(self.PIN)

    @property
    def running(self):
        return bool(mem32[self.CLOCKS_CLK_GPOUT1_CTRL] & self.CTRL_ENABLE)

    def start(self, divider=4):
        divider = int(divider)
        if not 1 <= divider <= 0xFFFFFF:
            raise ValueError("FPGA clock divider must be 1..16777215")

        # Program the divider before routing and enabling the clock output.
        mem32[self.CLOCKS_CLK_GPOUT1_CTRL] = 0
        mem32[self.CLOCKS_CLK_GPOUT1_DIV] = divider << 8
        gpio_ctrl = self.IO_BANK0_GPIO_CTRL + self.PIN * 8
        mem32[gpio_ctrl] = (mem32[gpio_ctrl] & ~0x1F) | self.GPIO_FUNC_GPCK
        mem32[self.CLOCKS_CLK_GPOUT1_CTRL] = (
            self.AUXSRC_CLK_SYS | self.CTRL_DC50 | self.CTRL_ENABLE
        )
        return 125_000_000 // divider

    def stop(self):
        mem32[self.CLOCKS_CLK_GPOUT1_CTRL] &= ~self.CTRL_ENABLE
        self.pin.init(Pin.IN)


class FW1FPGAProgrammer:
    """Program only volatile UP5K CRAM through the stock SPI1 wiring."""

    SPI_ID = 1
    MISO_PIN = 12
    CS_PIN = 13
    SCK_PIN = 14
    MOSI_PIN = 15
    CDONE_PIN = 24
    CRESET_PIN = 29

    BAUDRATE = 5_000_000
    MIN_SIZE = 64 * 1024
    MAX_SIZE = 128 * 1024
    SYNC_WORD = b"\x7e\xaa\x99\x7e"
    PRE_CLEAR_US = 2
    CLEAR_US = 1_300
    PREAMBLE_CLOCK_BYTES = 1
    CDONE_CLOCK_BYTES = 13
    WAKE_CLOCK_BYTES = 7

    def __init__(self):
        # Constructing the programmer does not reset or reconfigure the FPGA.
        self.miso = Pin(self.MISO_PIN)
        self.cs = Pin(self.CS_PIN)
        self.sck = Pin(self.SCK_PIN)
        self.mosi = Pin(self.MOSI_PIN)
        self.cdone = Pin(self.CDONE_PIN, Pin.IN)
        self.creset = Pin(self.CRESET_PIN)
        self.spi = None

    @staticmethod
    def _sha256(data):
        return "".join("{:02x}".format(byte) for byte in hashlib.sha256(data).digest())

    @classmethod
    def validate(cls, bitstream):
        size = len(bitstream)
        if not cls.MIN_SIZE <= size <= cls.MAX_SIZE:
            raise ValueError("unexpected UP5K bitstream size {}".format(size))
        if cls.SYNC_WORD not in bitstream[:256]:
            raise ValueError("iCE40 synchronization word not found")
        return {"size": size, "sha256": cls._sha256(bitstream)}

    def _open_spi(self):
        self.spi = SPI(
            self.SPI_ID,
            baudrate=self.BAUDRATE,
            polarity=0,
            phase=0,
            bits=8,
            firstbit=SPI.MSB,
            sck=self.sck,
            mosi=self.mosi,
            miso=self.miso,
        )
        return self.spi

    def _close_spi(self):
        if self.spi is not None:
            self.spi.deinit()
            self.spi = None

    def _release_configuration_pins(self):
        self._close_spi()
        self.cs.init(Pin.IN)
        self.sck.init(Pin.IN)
        self.mosi.init(Pin.IN)
        self.miso.init(Pin.IN)
        self.creset.init(Pin.IN)

    def hold_reset(self):
        self._close_spi()
        self.cs.init(Pin.OUT, value=0)
        self.sck.init(Pin.OUT, value=1)
        self.mosi.init(Pin.OUT, value=0)
        self.creset.init(Pin.OUT, value=0)

    def restore_nvcm(self, timeout_ms=20):
        """Request the factory NVCM image without writing nonvolatile state."""
        self._close_spi()
        self.cs.init(Pin.OUT, value=1)
        self.sck.init(Pin.OUT, value=1)
        self.mosi.init(Pin.OUT, value=0)
        self.creset.init(Pin.OUT, value=0)
        time.sleep_us(self.PRE_CLEAR_US)
        self.creset.value(1)
        self.creset.init(Pin.IN)

        deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
        while not self.cdone.value() and time.ticks_diff(deadline, time.ticks_ms()) > 0:
            time.sleep_ms(1)
        configured = bool(self.cdone.value())
        if configured:
            self._release_configuration_pins()
        else:
            self.hold_reset()
        return configured

    def program(self, bitstream, restore_on_failure=True):
        metadata = self.validate(bitstream)
        started = time.ticks_ms()
        prior_cdone = bool(self.cdone.value())

        try:
            # Match the stock reset sequence: SS low, SCK high, then release
            # CRESET_B and wait for the UP5K CRAM clear interval.
            self.cs.init(Pin.OUT, value=0)
            self.sck.init(Pin.OUT, value=1)
            self.mosi.init(Pin.OUT, value=0)
            self.creset.init(Pin.OUT, value=0)
            time.sleep_us(self.PRE_CLEAR_US)
            self.creset.value(1)
            self.creset.init(Pin.IN)
            time.sleep_us(self.CLEAR_US)

            self.cs.value(1)
            if self.cdone.value():
                raise OSError("CDONE stayed high after configuration reset")

            spi = self._open_spi()
            spi.write(b"\x00" * self.PREAMBLE_CLOCK_BYTES)
            self.cs.value(0)
            spi.write(bitstream)
            self.cs.value(1)

            spi.write(b"\x00" * self.CDONE_CLOCK_BYTES)
            if not self.cdone.value():
                raise OSError("CDONE did not rise after bitstream")

            # UP5K user I/O becomes active after at least 49 more clocks.
            spi.write(b"\x00" * self.WAKE_CLOCK_BYTES)
            if not self.cdone.value():
                raise OSError("CDONE fell during wake clocks")

            self._release_configuration_pins()
            metadata.update(
                cdone=True,
                prior_cdone=prior_cdone,
                elapsed_ms=time.ticks_diff(time.ticks_ms(), started),
            )
            return metadata
        except Exception:
            self.hold_reset()
            if restore_on_failure:
                self.restore_nvcm()
            raise

    def program_file(self, path, restore_on_failure=True):
        with open(path, "rb") as source:
            bitstream = source.read(self.MAX_SIZE + 1)
        return self.program(bitstream, restore_on_failure=restore_on_failure)

    def status(self):
        return {
            "cdone": bool(self.cdone.value()),
            "reset_held": self.creset.value() == 0,
        }
