#pragma once

#include <cmath>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <string>

#include "esp_ota_ops.h"
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

// True on the first boot of a freshly uploaded image, before it has been
// marked valid. Such a boot is the slowest one the node ever performs: the
// flash was just rewritten and cached fast-connect data may be stale, forcing
// a full scan. It is also the only boot where sleeping early is unrecoverable
// -- the bootloader reverts an image that never validated, so the update is
// silently lost and the node comes back on the old firmware.
inline bool soil_ota_pending_verify() {
  const esp_partition_t *running = esp_ota_get_running_partition();
  esp_ota_img_states_t state;
  if (running != nullptr &&
      esp_ota_get_state_partition(running, &state) == ESP_OK) {
    return state == ESP_OTA_IMG_PENDING_VERIFY;
  }
  return false;
}

// Sleep duration for a wake that never reached MQTT. A failed wake costs the
// whole radio-retry window at full power and produces nothing, so an outage
// that lasts hours must not buy one full retry every normal interval.
//
// streak 1     -> base (a single missed wake is normal; do not penalise it)
// streak 2..4  -> base << (streak - 1), i.e. 2x, 4x, 8x
// streak 5+    -> capped
//
// Callers should keep cap_ms under whatever staleness alert watches the node,
// so one that recovers is not still asleep when the alert fires.
inline uint32_t soil_offline_backoff_ms(uint32_t base_ms, int streak,
                                        uint32_t cap_ms) {
  if (streak < 2) {
    return base_ms;
  }
  uint32_t shift = static_cast<uint32_t>(streak - 1);
  if (shift > 3) {
    shift = 3;
  }
  const uint64_t backoff = static_cast<uint64_t>(base_ms) << shift;
  return backoff > cap_ms ? cap_ms : static_cast<uint32_t>(backoff);
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
