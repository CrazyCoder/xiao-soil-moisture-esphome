#pragma once
// Duracell MN1500 AA alkaline discharge curve for this sensor's load profile.
//
// Replaces the old linear 1.2-1.5 V map, which pinned a fresh cell at 100% for
// weeks and then fell through the entire scale in ~12 days: an alkaline spends
// most of its life above 1.5 V and most of the rest between 1.5 and 1.2 V, so a
// 0.3 V window reports "full" until the cell is nearly gone.
//
// Points are remaining service derived from Duracell's official MN1500 50 mW
// constant-power graph, normalized to the sensor's practical 0.9 V cutoff.
// This is deliberately the lightest published loaded curve: the ADC is sampled
// before WiFi starts, after the cell has recovered during deep sleep. A current
// Coppertop cell should perform at least as well as this older/basic MN1500
// reference, but temperature, cell age and pulse impedance still make any
// voltage-only percentage approximate. Trend the published raw volts as the
// authoritative signal.
//
// Source: Duracell Global Product Technical Data Sheets, MN1500 AA (LR6),
// "Constant Power", 50 mW, 21 C:
// https://duracell.com/techlibrary/product-technical-data-sheets
#include <cmath>
#include <cstddef>

static inline float alkaline_aa_percent(float volts) {
  static const float kVolts[] = {
      0.90f, 0.95f, 1.00f, 1.05f, 1.10f, 1.15f, 1.20f, 1.25f,
      1.30f, 1.35f, 1.40f, 1.45f, 1.50f, 1.55f, 1.60f};
  static const float kPercent[] = {
      0.0f,  1.0f,  3.0f,  6.0f,  11.0f, 16.0f, 24.0f, 37.0f,
      57.0f, 73.0f, 83.0f, 91.0f, 96.0f, 98.0f, 100.0f};
  const size_t n = sizeof(kVolts) / sizeof(kVolts[0]);

  if (std::isnan(volts) || volts <= 0.0f)
    return NAN;                       // no ADC reading yet - publish nothing useful
  if (volts <= kVolts[0])
    return 0.0f;
  if (volts >= kVolts[n - 1])
    return 100.0f;

  for (size_t i = 1; i < n; i++) {
    if (volts < kVolts[i]) {
      const float span = kVolts[i] - kVolts[i - 1];
      const float t = (volts - kVolts[i - 1]) / span;
      return kPercent[i - 1] + t * (kPercent[i] - kPercent[i - 1]);
    }
  }
  return 100.0f;
}
