# RGB effect provenance

`display/lib/fw1_effects.py` is a clean, MicroPython-oriented implementation
inspired by two MIT-licensed projects. It does not vendor their C++ sources.

- WS2812FX: https://github.com/kitesurfer1404/WS2812FX
  - Observed commit: `6dfa94eedf2503d0e5cd4c8860c8db77cea46182`
  - Copyright (c) 2016 Harm Aldick
  - License: MIT
  - Inspired modes: static, blink, breath, color wipe, rainbow cycle, theater
    chase, twinkle fade, Larson scanner, and fire flicker.
- FastLED Pride2015:
  https://github.com/FastLED/FastLED/blob/master/examples/Pride2015/Pride2015.ino
  - Observed file commit: `19353a40f7812d4c9ab9f66516d9c943729fdfd3`
  - Original example by Mark Kriegsman
  - Copyright (c) 2013 FastLED
  - License: MIT
  - Adapted concepts: fixed-point beat oscillators, pseudotime and hue
    accumulation, reverse traversal, and persistent quarter blending.

The applicable MIT notices are retained in `LICENSE` in this directory.
