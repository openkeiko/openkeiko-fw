"""Small, nonblocking RGB effects engine for the seven-pixel FW1 chain.

The effect selection is inspired by WS2812FX (Copyright (c) 2016 Harm
Aldick, MIT). Pride 2015 is an integer adaptation of Mark Kriegsman's example
from FastLED (Copyright (c) 2013 FastLED, MIT). This is a clean Python
implementation, not a copy of either source.

Colors passed to this module are RGB. The WS2812 driver remains responsible
for GRB packing, output inversion, and applying its global brightness.
"""


PIXEL_COUNT = 7

MODE_STATIC = 0
MODE_BLINK = 1
MODE_BREATH = 2
MODE_COLOR_WIPE = 3
MODE_RAINBOW_CYCLE = 4
MODE_THEATER_CHASE = 5
MODE_TWINKLE_FADE = 6
MODE_LARSON_SCANNER = 7
MODE_FIRE_FLICKER = 8
MODE_PRIDE_2015 = 9

MODE_NAMES = (
    "Static",
    "Blink",
    "Breath",
    "Color Wipe",
    "Rainbow Cycle",
    "Theater Chase",
    "Twinkle Fade",
    "Larson Scanner",
    "Fire Flicker",
    "Pride 2015",
)

_TICKS_PERIOD = 1 << 30
_TICKS_HALF = _TICKS_PERIOD >> 1
_BLACK = 0
_WHITE = 0xFFFFFF


def _ticks_diff(new, old):
    """MicroPython ticks_diff semantics without importing time."""
    return ((int(new) - int(old) + _TICKS_HALF) % _TICKS_PERIOD) - _TICKS_HALF


def _color(value):
    if len(value) != 3:
        raise ValueError("expected an RGB tuple")
    red = max(0, min(255, int(value[0])))
    green = max(0, min(255, int(value[1])))
    blue = max(0, min(255, int(value[2])))
    return (red << 16) | (green << 8) | blue


def _scale(color, level):
    return (
        (((color >> 16) & 0xFF) * level // 255) << 16
        | (((color >> 8) & 0xFF) * level // 255) << 8
        | ((color & 0xFF) * level // 255)
    )


def _blend(old, new, amount):
    keep = 255 - amount
    red = (((old >> 16) & 0xFF) * keep + ((new >> 16) & 0xFF) * amount) // 255
    green = (((old >> 8) & 0xFF) * keep + ((new >> 8) & 0xFF) * amount) // 255
    blue = ((old & 0xFF) * keep + (new & 0xFF) * amount) // 255
    return (red << 16) | (green << 8) | blue


def _wheel(position):
    position &= 0xFF
    if position < 85:
        return ((255 - position * 3) << 16) | (position * 3 << 8)
    if position < 170:
        position -= 85
        return ((255 - position * 3) << 8) | (position * 3)
    position -= 170
    return (position * 3 << 16) | (255 - position * 3)


def _sin16(theta):
    """Fast fixed-point parabolic sine approximation, output -32768..32768."""
    theta &= 0xFFFF
    negative = theta >= 0x8000
    x = theta & 0x7FFF
    value = (x * (0x8000 - x)) >> 13
    return -value if negative else value


def _beatsin88(now_ms, bpm88, low, high):
    # bpm88 is beats per minute times 256, as in FastLED.
    phase = (int(now_ms) * int(bpm88) * 280) >> 16
    wave = _sin16(phase) + 32768
    return int(low) + ((int(high) - int(low)) * wave >> 16)


def _hsv(hue, saturation, value):
    """Convert 8-bit HSV to packed RGB using integer arithmetic."""
    hue &= 0xFF
    saturation &= 0xFF
    value &= 0xFF
    if saturation == 0:
        return (value << 16) | (value << 8) | value

    region = (hue * 6) >> 8
    fraction = (hue * 6) & 0xFF
    p = value * (255 - saturation) // 255
    q = value * (255 - saturation * fraction // 255) // 255
    t = value * (255 - saturation * (255 - fraction) // 255) // 255
    if region == 0:
        red, green, blue = value, t, p
    elif region == 1:
        red, green, blue = q, value, p
    elif region == 2:
        red, green, blue = p, value, t
    elif region == 3:
        red, green, blue = p, q, value
    elif region == 4:
        red, green, blue = t, p, value
    else:
        red, green, blue = value, p, q
    return (red << 16) | (green << 8) | blue


class FW1Effects:
    """Drive curated effects on an existing seven-pixel ``WS2812`` object.

    Call ``update(time.ticks_ms())`` frequently. It returns True only when a
    frame was sent. ``speed`` is 0..255 (slow to fast). ``brightness`` uses the
    driver's 0.0..1.0 range and does not alter its electrical inversion mode.
    A mode can be selected by its integer constant or case-insensitive name.
    """

    def __init__(
        self,
        pixels,
        mode=MODE_STATIC,
        color=(255, 96, 12),
        background=(0, 0, 0),
        speed=128,
        brightness=None,
        seed=1,
    ):
        if len(pixels) != PIXEL_COUNT:
            raise ValueError("FW1 effects require exactly seven pixels")
        self.pixels = pixels
        self._frame = [_BLACK] * PIXEL_COUNT
        self._color = _color(color)
        self._background = _color(background)
        self._speed = 128
        self._mode = MODE_STATIC
        self._step = 0
        self._last_ms = None
        self._dirty = True
        self._seed = int(seed) & 0xFFFFFFFF
        if self._seed == 0:
            self._seed = 1
        self._initial_seed = self._seed
        self._pride_time = 0
        self._pride_pseudo = 0
        self._pride_hue = 0
        self.speed = speed
        if brightness is not None:
            self.brightness = brightness
        self.set_mode(mode)

    @property
    def mode(self):
        return self._mode

    @property
    def mode_name(self):
        return MODE_NAMES[self._mode]

    @property
    def speed(self):
        return self._speed

    @speed.setter
    def speed(self, value):
        self._speed = max(0, min(255, int(value)))

    @property
    def brightness(self):
        return self.pixels.brightness

    @brightness.setter
    def brightness(self, value):
        value = max(0.0, min(1.0, float(value)))
        self.pixels.brightness = value
        self._dirty = True

    @property
    def color(self):
        return (
            (self._color >> 16) & 0xFF,
            (self._color >> 8) & 0xFF,
            self._color & 0xFF,
        )

    @color.setter
    def color(self, value):
        self._color = _color(value)
        self._dirty = True

    @property
    def background(self):
        return (
            (self._background >> 16) & 0xFF,
            (self._background >> 8) & 0xFF,
            self._background & 0xFF,
        )

    @background.setter
    def background(self, value):
        self._background = _color(value)
        self._dirty = True

    @classmethod
    def mode_names(cls):
        return MODE_NAMES

    @staticmethod
    def _normalize_name(name):
        return "".join(character.lower() for character in name if character.isalnum())

    def set_mode(self, mode):
        if isinstance(mode, str):
            wanted = self._normalize_name(mode)
            selected = -1
            for index, name in enumerate(MODE_NAMES):
                if self._normalize_name(name) == wanted:
                    selected = index
                    break
            if selected < 0:
                raise ValueError("unknown effect mode: " + mode)
            mode = selected
        mode = int(mode)
        if mode < 0 or mode >= len(MODE_NAMES):
            raise ValueError("effect mode out of range")
        self._mode = mode
        self._reset()
        return self._mode

    def next_mode(self):
        return self.set_mode((self._mode + 1) % len(MODE_NAMES))

    def _reset(self):
        self._step = 0
        self._last_ms = None
        self._dirty = True
        self._seed = self._initial_seed
        self._pride_time = 0
        self._pride_pseudo = 0
        self._pride_hue = 0
        for index in range(PIXEL_COUNT):
            self._frame[index] = _BLACK

    def _random(self):
        # LCG makes effects repeatable across Python and MicroPython builds.
        self._seed = (1664525 * self._seed + 1013904223) & 0xFFFFFFFF
        return self._seed

    def _interval(self):
        speed = self._speed
        mode = self._mode
        if mode == MODE_BLINK:
            return 120 + (255 - speed) * 4
        if mode == MODE_BREATH or mode == MODE_PRIDE_2015:
            return 25
        if mode == MODE_COLOR_WIPE:
            return max(30, 320 - speed)
        if mode == MODE_RAINBOW_CYCLE:
            return max(20, 220 - speed * 3 // 4)
        if mode == MODE_THEATER_CHASE:
            return max(35, 270 - speed * 4 // 5)
        if mode == MODE_TWINKLE_FADE:
            return max(35, 220 - speed * 2 // 3)
        if mode == MODE_LARSON_SCANNER:
            return max(30, 210 - speed * 2 // 3)
        if mode == MODE_FIRE_FLICKER:
            return max(35, 160 - speed // 2)
        return 1000

    def update(self, now_ms):
        """Render at most one due frame; never waits for a future deadline."""
        now_ms = int(now_ms) % _TICKS_PERIOD
        if self._mode == MODE_STATIC and not self._dirty:
            return False

        if self._last_ms is None:
            elapsed = 0
        else:
            elapsed = _ticks_diff(now_ms, self._last_ms)
            if elapsed < 0 or (elapsed < self._interval() and not self._dirty):
                return False
        self._last_ms = now_ms

        mode = self._mode
        if mode == MODE_STATIC:
            self._fill(self._color)
        elif mode == MODE_BLINK:
            self._fill(self._color if (self._step & 1) == 0 else self._background)
        elif mode == MODE_BREATH:
            self._breath()
        elif mode == MODE_COLOR_WIPE:
            self._color_wipe()
        elif mode == MODE_RAINBOW_CYCLE:
            self._rainbow_cycle()
        elif mode == MODE_THEATER_CHASE:
            self._theater_chase()
        elif mode == MODE_TWINKLE_FADE:
            self._twinkle_fade()
        elif mode == MODE_LARSON_SCANNER:
            self._larson_scanner()
        elif mode == MODE_FIRE_FLICKER:
            self._fire_flicker()
        else:
            self._pride(elapsed)

        self._flush()
        self._step = (self._step + 1) & 0xFFFF
        self._dirty = False
        return True

    def _fill(self, color):
        for index in range(PIXEL_COUNT):
            self._frame[index] = color

    def _flush(self):
        # RGB tuples deliberately leave GRB packing and inversion to WS2812.
        for index in range(PIXEL_COUNT):
            color = self._frame[index]
            self.pixels[index] = (
                (color >> 16) & 0xFF,
                (color >> 8) & 0xFF,
                color & 0xFF,
            )
        self.pixels.show()

    def _breath(self):
        phase = (self._step * (1 + self._speed // 32) * 256) & 0xFFFF
        wave = _sin16(phase) + 32768
        # Squaring makes the low end linger like a standby indicator.
        level = ((wave * wave) >> 16) * 255 >> 16
        self._fill(_blend(self._background, self._color, level))

    def _color_wipe(self):
        position = self._step % (PIXEL_COUNT * 2)
        lit = position + 1 if position < PIXEL_COUNT else PIXEL_COUNT * 2 - position - 1
        for index in range(PIXEL_COUNT):
            self._frame[index] = self._color if index < lit else self._background

    def _rainbow_cycle(self):
        offset = self._step * (1 + self._speed // 32)
        for index in range(PIXEL_COUNT):
            self._frame[index] = _wheel(offset + index * 256 // PIXEL_COUNT)

    def _theater_chase(self):
        phase = self._step % 3
        for index in range(PIXEL_COUNT):
            self._frame[index] = self._color if (index + phase) % 3 == 0 else self._background

    def _twinkle_fade(self):
        for index in range(PIXEL_COUNT):
            self._frame[index] = _blend(self._background, self._frame[index], 174)
        random_value = self._random()
        index = (random_value >> 16) % PIXEL_COUNT
        self._frame[index] = _wheel(random_value >> 24)

    def _larson_scanner(self):
        span = (PIXEL_COUNT - 1) * 2
        head = self._step % span
        if head >= PIXEL_COUNT:
            head = span - head
        for index in range(PIXEL_COUNT):
            distance = abs(index - head)
            if distance == 0:
                level = 255
            elif distance == 1:
                level = 72
            elif distance == 2:
                level = 16
            else:
                level = 0
            self._frame[index] = _blend(self._background, self._color, level)

    def _fire_flicker(self):
        for index in range(PIXEL_COUNT):
            random_value = self._random()
            heat = 150 + ((random_value >> 16) & 0x69)
            if heat > 255:
                heat = 255
            red = heat
            green = max(0, heat - 72 - ((random_value >> 8) & 0x1F))
            blue = max(0, (heat - 205) // 3)
            self._frame[index] = (red << 16) | (green << 8) | blue

    def _pride(self, elapsed_ms):
        # At speed 128 virtual and wall time match; extremes are 0.5x and 1.5x.
        delta = max(0, min(1000, elapsed_ms)) * (128 + self._speed) // 256
        self._pride_time = (self._pride_time + delta) & 0xFFFFFFFF
        now = self._pride_time
        saturation = _beatsin88(now, 87, 220, 250)
        bright_depth = _beatsin88(now, 341, 96, 224)
        bright_inc = _beatsin88(now, 203, 25 * 256, 40 * 256)
        time_multiplier = _beatsin88(now, 147, 23, 60)
        hue_inc = _beatsin88(now, 113, 1, 3000)
        self._pride_pseudo = (
            self._pride_pseudo + delta * time_multiplier
        ) & 0xFFFF
        self._pride_hue = (
            self._pride_hue + delta * _beatsin88(now, 400, 5, 9)
        ) & 0xFFFF
        brightness_theta = self._pride_pseudo
        hue = self._pride_hue

        for index in range(PIXEL_COUNT):
            hue = (hue + hue_inc) & 0xFFFF
            brightness_theta = (brightness_theta + bright_inc) & 0xFFFF
            b16 = _sin16(brightness_theta) + 32768
            bri16 = (b16 * b16) >> 16
            brightness = ((bri16 * bright_depth) >> 16) + 255 - bright_depth
            new_color = _hsv(hue >> 8, saturation, brightness)
            pixel = PIXEL_COUNT - 1 - index
            self._frame[pixel] = _blend(self._frame[pixel], new_color, 64)
