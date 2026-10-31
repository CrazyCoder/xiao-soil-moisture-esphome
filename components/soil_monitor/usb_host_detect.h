#pragma once
// ESP32-C6 native USB-Serial-JTAG host detection.
// An enumerated USB host drives SOF frames at 1 kHz, ticking the frame-number
// register; a charger/power bank never sends SOF.
//
// WHY THIS IS NOT A BARE `f1 != f2` COMPARISON
// A positive result latches usb_hold, and usb_hold calls prevent_deep_sleep().
// prevent_deep_sleep() permanently disarms the run_duration cap: begin_sleep()
// only latches next_enter_deep_sleep_ and loop() spins until allow_deep_sleep()
// is called (deep_sleep_component.cpp). A single glitched read of this register
// can therefore hold the node awake with the radio powered until the cell is
// flat - orders of magnitude above the deep-sleep budget, and silent, because
// a node that never sleeps also never reports.
//
// Two defences, both cheap:
//   1. Require a PLAUSIBLE delta, not merely a different value. A live 1 kHz
//      SOF stream advances the counter by ~20 frames per 20 ms window; noise,
//      a partially clocked peripheral or a torn read does not.
//   2. Require several consecutive plausible windows before asserting.
#include <cstdint>

#include "soc/soc.h"
#include "soc/usb_serial_jtag_reg.h"
#include "esp_rom_sys.h"

// SOF frame index is USB_SERIAL_JTAG_SOF_FRAME_INDEX, bits [10:0] -> 11 bits,
// 0..2047, wrapping every 2.048 s. (The pre-1.5.0 comment here said 12 bits.)
static constexpr uint32_t USB_SOF_FRAME_MASK = USB_SERIAL_JTAG_SOF_FRAME_INDEX;
static constexpr uint32_t USB_SOF_WINDOW_US = 20000;
// 20 ms at 1 kHz = 20 frames. The bounds absorb interrupt jitter on either
// read while still rejecting a static (0) or nonsense delta.
static constexpr uint32_t USB_SOF_MIN_DELTA = 12;
static constexpr uint32_t USB_SOF_MAX_DELTA = 30;
// Acquire is strict, release is lenient: a missed acquire costs one report
// cycle, but a spurious release deep-sleeps the node mid-flash.
//
// Two windows, not three: 3 x 20 ms exceeds ESPHome's 50 ms component-loop
// warning threshold and logs "took a long time for an operation (60 ms)" on
// every check. The plausibility test does the real work here - two consecutive
// windows that both look like a live 1 kHz stream is already an extremely
// strong signal, and nothing short of a real SOF source produces it twice.
static constexpr int USB_SOF_ACQUIRE_WINDOWS = 2;
static constexpr int USB_SOF_RELEASE_WINDOWS = 2;

// One 20 ms observation window. True only when the frame counter advanced by
// an amount consistent with a live 1 kHz SOF stream.
static inline bool usb_sof_window_plausible() {
  const uint32_t f1 = REG_READ(USB_SERIAL_JTAG_FRAM_NUM_REG) & USB_SOF_FRAME_MASK;
  esp_rom_delay_us(USB_SOF_WINDOW_US);
  const uint32_t f2 = REG_READ(USB_SERIAL_JTAG_FRAM_NUM_REG) & USB_SOF_FRAME_MASK;
  const uint32_t delta = (f2 - f1) & USB_SOF_FRAME_MASK;
  return delta >= USB_SOF_MIN_DELTA && delta <= USB_SOF_MAX_DELTA;
}

// Acquire test (~60 ms): every window must look like a live SOF stream.
// Only call this where taking a hold is actually wanted.
static inline bool usb_host_present_confirmed() {
  for (int i = 0; i < USB_SOF_ACQUIRE_WINDOWS; i++) {
    if (!usb_sof_window_plausible()) {
      return false;
    }
  }
  return true;
}

// Release test (<=40 ms): keep the hold if ANY window still looks live, so one
// jittered window cannot drop a genuine flashing session. Once the cable is
// out every window fails, so a real unplug still releases on the next poll.
static inline bool usb_host_still_present() {
  for (int i = 0; i < USB_SOF_RELEASE_WINDOWS; i++) {
    if (usb_sof_window_plausible()) {
      return true;
    }
  }
  return false;
}
