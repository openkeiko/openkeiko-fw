# Keep the board definition outside the MicroPython source checkout.
list(APPEND PICO_BOARD_HEADER_DIRS ${MICROPY_BOARD_DIR})

set(PICO_BOARD "fw1_16mb")
set(PICO_PLATFORM "rp2040")
