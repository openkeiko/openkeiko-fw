"""OpenKeiko FW1 board wrapper for the upstream st7789py driver."""

from machine import Pin, PWM, SPI
import time

import st7789py as st7789


class FW1TFT:
    WIDTH = 320
    HEIGHT = 240

    def __init__(self, brightness=0.65, baudrate=24_000_000):
        # Stock v67 holds GPIO8 low before initializing the display.
        self.enable = Pin(8, Pin.OUT, value=0)
        self.backlight_pin = Pin(25, Pin.OUT, value=1)
        time.sleep_ms(100)

        self.spi = SPI(
            1,
            baudrate=baudrate,
            polarity=0,
            phase=0,
            bits=8,
            firstbit=SPI.MSB,
            sck=Pin(10),
            mosi=Pin(11),
            miso=None,
        )
        self.panel = st7789.ST7789(
            self.spi,
            240,
            320,
            reset=None,
            dc=Pin(12, Pin.OUT),
            cs=Pin(13, Pin.OUT, value=1),
            backlight=None,
            rotation=1,
        )

        self.backlight = PWM(self.backlight_pin)
        self.backlight.freq(1_000)
        self.set_brightness(brightness)

    def set_brightness(self, brightness):
        if brightness < 0.0:
            brightness = 0.0
        elif brightness > 1.0:
            brightness = 1.0
        self.brightness = brightness
        self.backlight.duty_u16(int(brightness * 65_535))

    def color_bars(self):
        colors = (st7789.RED, st7789.GREEN, st7789.BLUE, st7789.WHITE)
        width = self.panel.width // len(colors)
        for index, color in enumerate(colors):
            x = index * width
            self.panel.fill_rect(x, 0, width, self.panel.height, color)

    def deinit(self):
        self.panel.fill(st7789.BLACK)
        self.backlight.deinit()
        self.backlight_pin.off()
        self.spi.deinit()
