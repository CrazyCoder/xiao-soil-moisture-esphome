#pragma once
// ESP32-C6 native USB-Serial-JTAG host detection.
// An enumerated USB host drives SOF frames at 1 kHz, ticking the frame-number
// register; a charger/power bank never sends SOF. Two reads 20 ms apart differ
// iff a host is attached (12-bit counter can't alias back in 20 ms).
#include "soc/soc.h"
#include "soc/usb_serial_jtag_reg.h"
#include "esp_rom_sys.h"

static inline bool usb_host_present() {
  uint32_t f1 = REG_READ(USB_SERIAL_JTAG_FRAM_NUM_REG);
  esp_rom_delay_us(20000);
  uint32_t f2 = REG_READ(USB_SERIAL_JTAG_FRAM_NUM_REG);
  return f1 != f2;
}
