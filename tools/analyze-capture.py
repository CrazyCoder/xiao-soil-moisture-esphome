# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy"]
# ///
"""
Break a ppk2-monitor CSV into its boot/report burst and its standby floor.

    uv run analyze-capture.py plant5-standby.csv
"""
import sys

import numpy as np

path = sys.argv[1] if len(sys.argv) > 1 else "plant5-standby.csv"
d = np.loadtxt(path, delimiter=",", skiprows=1)
t, i = d[:, 0], d[:, 1]
rate = len(t) / (t[-1] - t[0])
print(f"{len(t)} samples, {t[-1]:.2f} s, {rate/1000:.1f} kSa/s")

# Standby floor = median of the last third (device is long asleep by then).
floor = np.median(i[-len(i) // 3:])

# Sleep entry, on 100 ms block means rather than raw samples: a sleeping node
# still emits short converter bursts (plant 1 hits 4.8 mA against a 92 uA floor),
# so any per-sample threshold latches onto those and reports a bogus late entry.
blk = int(rate * 0.1)
nblk = len(i) // blk
means = i[: nblk * blk].reshape(nblk, blk).mean(axis=1)
busy = np.flatnonzero(means > floor * 5)
t_sleep = (busy[-1] + 1) * 0.1 if len(busy) else 0.0

boot = i[t <= t_sleep]
# Subtract what the standby floor would have drawn over the same span, so this
# is the marginal cost of the wake rather than wake + idle.
q_boot_uC = (boot.sum() / rate - floor * t_sleep) if len(boot) else 0.0
tail = i[t > t_sleep]

print(f"\nsleep entry   : {t_sleep:.2f} s after power-on")
print(f"boot+report   : avg {boot.mean():.1f} uA, peak {boot.max()/1000:.1f} mA, "
      f"charge {q_boot_uC:.0f} uC ({q_boot_uC/3.6e6:.5f} mAh)")
# Mean is the right number for battery life (it is total charge); median is the
# better estimate of the quiescent floor when periodic bursts skew the mean.
print(f"standby       : avg {tail.mean():.1f} uA, median {np.median(tail):.1f} uA, "
      f"p5/p95 {np.percentile(tail,5):.0f}/{np.percentile(tail,95):.0f} uA")
if len(sys.argv) > 2:  # optional Vin in mV -> standby power, for I-vs-V classification
    vin_mv = float(sys.argv[2])
    print(f"standby power : {tail.mean() * vin_mv / 1e6:.3f} mW mean, "
          f"{np.median(tail) * vin_mv / 1e6:.3f} mW median  (at {vin_mv:.0f} mV)")

# Daily budgets for the notification-first schedule introduced 2026-07-27.
# The current state determines the next wake: Normal 2h, Almost Dry 1h, Dry 4h.
for state, wakes in (("Normal Moisture", 12), ("Almost Dry", 24), ("Dry", 6)):
    day = tail.mean() * 24 / 1000 + q_boot_uC / 3.6e6 * wakes
    print(f"  {state:15} ({wakes:2d} wakes/day): {day:.2f} mAh/day  "
          f"-> 2500 mAh AA lasts {2500/day:.0f} days")

# Ripple: dominant frequency of the standby segment.
seg = tail[: int(rate * 5)] if len(tail) > rate * 5 else tail
seg = seg - seg.mean()
if len(seg) > 16:
    spec = np.abs(np.fft.rfft(seg))
    freqs = np.fft.rfftfreq(len(seg), 1 / rate)
    spec[0] = 0
    peak = freqs[spec.argmax()]
    print(f"\nstandby ripple: {tail.max()-tail.min():.0f} uA pk-pk, "
          f"dominant {peak:.1f} Hz")
