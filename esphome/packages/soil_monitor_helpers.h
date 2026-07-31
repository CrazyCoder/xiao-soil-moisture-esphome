#pragma once

#include <cmath>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <string>

#include "esp_sleep.h"
#include "esp_system.h"
#include "esphome/core/build_info_data.h"
#include "esphome/core/defines.h"

// Calibration values are GPIO1 ADC voltages. Reject impossible values and
// near-zero spans so a bad probe/sample cannot make every reading look normal.
inline bool soil_calibration_valid(float dry, float wet) {
  return std::isfinite(dry) && std::isfinite(wet) && wet >= 0.0f &&
         dry <= 3.3f && (dry - wet) >= 0.15f;
}

inline const char *soil_wake_reason(esp_sleep_wakeup_cause_t cause) {
  switch (cause) {
    case ESP_SLEEP_WAKEUP_TIMER:
      return "Timer";
    case ESP_SLEEP_WAKEUP_GPIO:
      return "GPIO/button";
    case ESP_SLEEP_WAKEUP_EXT0:
      return "EXT0";
    case ESP_SLEEP_WAKEUP_EXT1:
      return "EXT1/button";
    case ESP_SLEEP_WAKEUP_TOUCHPAD:
      return "Touch";
    case ESP_SLEEP_WAKEUP_ULP:
      return "ULP";
    case ESP_SLEEP_WAKEUP_UART:
      return "UART";
    default:
      return "Cold boot/other";
  }
}

inline const char *soil_reset_reason(esp_reset_reason_t reason) {
  switch (reason) {
    case ESP_RST_POWERON:
      return "Power-on";
    case ESP_RST_SW:
      return "Software";
    case ESP_RST_PANIC:
      return "Panic";
    case ESP_RST_INT_WDT:
      return "Interrupt watchdog";
    case ESP_RST_TASK_WDT:
      return "Task watchdog";
    case ESP_RST_WDT:
      return "Other watchdog";
    case ESP_RST_DEEPSLEEP:
      return "Deep-sleep wake";
    case ESP_RST_BROWNOUT:
      return "Brownout";
    case ESP_RST_SDIO:
      return "SDIO";
    default:
      return "Unknown";
  }
}

inline std::string soil_firmware_build() {
  char value[48];
  std::snprintf(value, sizeof(value), "%s / 0x%08lx",
                ESPHOME_PROJECT_VERSION,
                static_cast<unsigned long>(esphome::ESPHOME_CONFIG_HASH));
  return value;
}
