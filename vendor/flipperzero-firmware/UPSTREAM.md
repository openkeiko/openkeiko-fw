# Flipper Zero firmware reference

OpenKeiko's Flipper-compatible `.ir` and `.sub` file handling, Sub-GHz preset terminology, and receive workflow were informed by the public upstream firmware repository:

- Repository: https://github.com/flipperdevices/flipperzero-firmware
- License: GNU General Public License v3.0; see the upstream repository

Relevant upstream references include the infrared and Sub-GHz file-format documentation and the CC1101 preset definitions. No Flipper Zero firmware source files are vendored here. OpenKeiko's implementation is independent MicroPython code with deliberately limited compatibility and receive-only Sub-GHz behavior.
