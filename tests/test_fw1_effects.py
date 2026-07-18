"""Host-side behavior checks for the MicroPython FW1 effects engine."""

import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = (
    Path(__file__).parents[1]
    / "display"
    / "lib"
    / "fw1_effects.py"
)
SPEC = importlib.util.spec_from_file_location("fw1_effects", MODULE_PATH)
fx = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fx)


class FakePixels:
    def __init__(self, count=7):
        self.values = [(0, 0, 0)] * count
        self.brightness = 0.08
        self.show_count = 0

    def __len__(self):
        return len(self.values)

    def __setitem__(self, index, color):
        self.values[index] = color

    def show(self):
        self.show_count += 1


class FW1EffectsTests(unittest.TestCase):
    def test_requires_fw1_pixel_count(self):
        with self.assertRaises(ValueError):
            fx.FW1Effects(FakePixels(8))

    def test_static_only_shows_when_dirty(self):
        pixels = FakePixels()
        effects = fx.FW1Effects(pixels, color=(1, 2, 3))
        self.assertTrue(effects.update(10))
        self.assertFalse(effects.update(20))
        self.assertEqual(pixels.values, [(1, 2, 3)] * 7)
        effects.brightness = 0.5
        self.assertTrue(effects.update(20))
        self.assertEqual(pixels.brightness, 0.5)

    def test_mode_names_and_timing_are_deterministic(self):
        pixels = FakePixels()
        effects = fx.FW1Effects(pixels, mode="blink", speed=128)
        self.assertEqual(effects.mode_name, "Blink")
        self.assertTrue(effects.update(100))
        first = tuple(pixels.values)
        self.assertFalse(effects.update(101))
        self.assertEqual(tuple(pixels.values), first)
        self.assertEqual(effects.next_mode(), fx.MODE_BREATH)

    def test_every_mode_produces_bounded_rgb(self):
        for mode in range(len(fx.MODE_NAMES)):
            pixels = FakePixels()
            effects = fx.FW1Effects(pixels, mode=mode, seed=123)
            effects.update(0)
            for now in range(25, 2000, 25):
                effects.update(now)
            self.assertGreater(pixels.show_count, 0, fx.MODE_NAMES[mode])
            for color in pixels.values:
                self.assertEqual(len(color), 3)
                self.assertTrue(all(0 <= channel <= 255 for channel in color))

    def test_random_effect_repeats_after_mode_reset(self):
        pixels = FakePixels()
        effects = fx.FW1Effects(pixels, mode=fx.MODE_TWINKLE_FADE, seed=9)
        effects.update(0)
        first = tuple(pixels.values)
        effects.update(1000)
        effects.set_mode(fx.MODE_TWINKLE_FADE)
        effects.update(0)
        self.assertEqual(tuple(pixels.values), first)

    def test_tick_wrap_is_nonblocking(self):
        pixels = FakePixels()
        effects = fx.FW1Effects(pixels, mode=fx.MODE_BREATH)
        effects.update((1 << 30) - 10)
        self.assertTrue(effects.update(20))


if __name__ == "__main__":
    unittest.main()
