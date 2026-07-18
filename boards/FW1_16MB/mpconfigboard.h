#define MICROPY_HW_BOARD_NAME "FW1 RP2040 16MB"

// Reserve the first MiB for firmware and expose the remaining 15 MiB as storage.
#define MICROPY_HW_FLASH_STORAGE_BYTES (15 * 1024 * 1024)
