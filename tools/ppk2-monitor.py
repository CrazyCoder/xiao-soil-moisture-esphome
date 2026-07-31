# /// script
# requires-python = ">=3.10"
# dependencies = ["ppk2-api>=0.9.2"]
# ///
"""
CLI current monitor for the Nordic Power Profiler Kit II.

Run with uv (deps resolve automatically, no venv needed):

    uv run ppk2-monitor.py info
    uv run ppk2-monitor.py measure --volt 1500 --seconds 60
    uv run ppk2-monitor.py measure --volt 1500 --seconds 10 --csv plant4-sleep.csv

Target: XIAO ESP32-C6 soil node running from a SINGLE alkaline AA through a
boost converter. Measure at the AA HOLDER TERMINALS, i.e. on the ~1.5 V side of
the boost, which is where the fleet's standby draw actually shows up.

*** Source voltage is ~1500 mV, NOT 3.3/3.7 V. The holder terminals feed the
boost converter input; a fresh LR6 is 1.6 V and the node dies near 0.9 V.
Applying 3.7 V there can destroy the converter. ***

GOTCHA - source mode is marginal at <=1.5 V (measured 2026-07-27). The boost
pulls >1.5 A peaks at boot, past the PPK2's 1 A range; the rail sags and the C6
brownout-resets forever, so the node never reaches deep sleep and you read
~220 mA instead of a standby floor. It sleeps reliably at 1800 mV. Either put a
470-1000 uF bulk cap across VOUT/GND at the board, measure at 1800 mV and scale
(the standby load is constant-power, so I is proportional to 1/Vin), or use
ampere mode with the real cell. A capture whose average is in the hundreds of mA
is a brownout loop, not a measurement.

Modes
-----
source  (default) PPK2 *supplies* the node and measures it.
        Wire: PPK2 VOUT -> holder + , PPK2 GND -> holder - , VIN unused.
        The AA cell must be out of the holder.

ampere  PPK2 sits in series with the real AA cell.
        Wire: cell + -> VIN , VOUT -> holder + , cell - and holder - -> GND.
        --volt is ignored in this mode.

Only one process may hold the PPK2 serial port: close the Power Profiler
desktop app before running this.

ACCURACY - this reads ~5-8% LOW against Nordic's Power Profiler app (measured
2026-07-27: unit 1 92 vs 96.31 uA, -4.5%; unit 4 458 vs 494.27 uA, -7.3%).
Calibration IS applied correctly and no samples are dropped; the likely cause is
that ppk2-api has no equivalent of the app's range-switch spike compensation, and
the error grows with how often the signal crosses measurement ranges (unit 4
swings 60 uA to 4.9 mA constantly, unit 1 barely moves). RATIOS are nearly
immune because the baseline carries the same bias - unit 4 is 5.13x baseline by
the app and 4.98x by this script. Use this for triage and unit-to-unit
comparison; use the app when you need the absolute number.
"""

import argparse
import csv
import statistics
import sys
import time

from ppk2_api.ppk2_api import PPK2_API, PPK2_Command


PPK2_MAX_UA = 1_000_000   # PPK2 top range is 1 A; anything above is not a real reading


def open_ppk(port):
    try:
        ppk = PPK2_API(port, timeout=1, write_timeout=1, exclusive=True)
    except Exception as e:
        if "Access is denied" in str(e) or "PermissionError" in str(e):
            sys.exit(
                f"error: {port} is held by another process.\n"
                "The nRF Connect Power Profiler app and this script cannot share the PPK2 - "
                "close the app (or stop its capture and eject the device) and retry."
            )
        sys.exit(f"error: cannot open {port}: {e}")
    if not ppk.get_modifiers():
        sys.exit(f"error: no PPK2 metadata on {port} (wrong port, or app still has it open)")
    return ppk


def read_samples(ppk, pending):
    """Read whatever the PPK2 has buffered; return (samples, leftover_bytes).

    Never hand ppk2-api a buffer shorter than one 4-byte frame. Its get_samples()
    builds `first_reading` from whatever it is given, so a 1-3 byte read yields a
    garbage sample AND sets remainder["len"] negative; the next call then derives a
    negative offset, skips 3 bytes and misaligns the stream until the following
    chunk. Accumulating here keeps every call frame-aligned.
    """
    raw = ppk.get_data()
    if raw:
        pending += raw
    if len(pending) < 4:
        return [], pending
    chunk, _ = ppk.get_samples(pending)
    return chunk, b""


def sanity_note(samples, rate):
    """Flag conditions that make a capture untrustworthy."""
    notes = []
    over = sum(1 for v in samples if v > PPK2_MAX_UA)
    if over:
        notes.append(
            f"{over} sample(s) above the PPK2's specified 1 A range "
            f"({100*over/len(samples):.3f}% of the capture). Not a software artefact - Nordic's "
            "own Power Profiler reports the same ~1.7 A boot peak - but accuracy is unspecified "
            "above 1 A, so treat peak values as indicative only"
        )
    if rate < 90_000:
        notes.append(
            f"effective sample rate {rate/1000:.1f} kSa/s is well below 100 - samples were "
            "dropped; close other software holding the port and retry"
        )
    return notes


def cmd_info(args):
    ppk = open_ppk(args.port)
    ppk._write_serial((PPK2_Command.GET_META_DATA,))
    print(ppk._read_metadata())


def cmd_measure(args):
    ppk = open_ppk(args.port)

    if args.mode == "source":
        ppk.use_source_meter()
        ppk.set_source_voltage(args.volt)
        print(f"source meter @ {args.volt} mV")
    else:
        ppk.use_ampere_meter()
        print("ampere meter (external supply via VIN)")

    # Sampling starts FIRST: applying power boots the node immediately (it reports
    # and then deep-sleeps), so powering on before sampling would lose the boot.
    ppk.start_measuring()
    if args.assume_powered:
        print("DUT power left as-is (assuming already booted / asleep)")
    else:
        ppk.toggle_DUT_power("ON")
        print("DUT power ON -> node will boot, report, then deep sleep")

    samples = []
    writer = fh = None
    if args.csv:
        fh = open(args.csv, "w", newline="", encoding="utf-8")
        writer = csv.writer(fh)
        writer.writerow(["t_s", "current_uA"])

    t0 = time.time()
    next_tick = t0 + 1.0
    pending = b""
    print(f"sampling {args.seconds}s ... (Ctrl+C to stop early)")
    try:
        while time.time() - t0 < args.seconds:
            chunk, pending = read_samples(ppk, pending)
            if chunk:
                samples.extend(chunk)
                if writer:
                    # Interpolate per-sample timestamps across the chunk. Stamping every
                    # sample with the wall-clock read time would quantise the CSV to the
                    # ~10 ms poll interval and make it non-monotonic within a chunk.
                    now = time.time() - t0
                    step = 1.0 / 100_000
                    t_start = now - len(chunk) * step
                    writer.writerows(
                        [(f"{t_start + i * step:.6f}", f"{v:.4f}") for i, v in enumerate(chunk)]
                    )
            if time.time() >= next_tick:
                next_tick += 1.0
                if samples:
                    recent = samples[-100_000:]
                    print(
                        f"  t={time.time() - t0:5.1f}s  "
                        f"avg={statistics.fmean(recent):10.2f} uA  "
                        f"min={min(recent):9.2f}  max={max(recent):10.2f}"
                    )
            time.sleep(0.01)
    except KeyboardInterrupt:
        print("interrupted")
    finally:
        ppk.stop_measuring()
        if args.keep_power:
            print("DUT power left ON (node stays asleep; re-run with --assume-powered)")
        else:
            ppk.toggle_DUT_power("OFF")
        if fh:
            fh.close()

    if not samples:
        sys.exit("error: no samples captured")

    elapsed = time.time() - t0
    rate = len(samples) / elapsed

    def report(title, data, span):
        avg = statistics.fmean(data)
        charge_uC = avg * span
        print(f"\n=== {title} ===")
        print(f"window       : {span:.2f} s  ({len(data)} samples @ {rate / 1000:.1f} kSa/s)")
        print(f"average      : {avg:.3f} uA")
        print(f"median       : {statistics.median(data):.3f} uA")
        print(f"min / max    : {min(data):.3f} / {max(data):.3f} uA")
        print(f"charge       : {charge_uC:.1f} uC  ({charge_uC / 3.6e6:.6f} mAh)")
        print(f"if continuous: {avg / 1000:.4f} mA  ->  {avg * 24 / 1000:.3f} mAh/day")

    report("full window (boot + report + sleep)", samples, elapsed)

    if args.settle > 0:
        cut = int(args.settle * rate)
        tail = samples[cut:]
        if tail:
            report(f"standby only (first {args.settle:g}s excluded)", tail, elapsed - args.settle)
        else:
            print(f"\nwarning: --settle {args.settle:g}s consumed the whole capture")

    for n in sanity_note(samples, rate):
        print(f"\nWARNING: {n}")

    if args.csv:
        print(f"\ncsv          : {args.csv}")


# Reference figures, measured 2026-07-27 (see ha/soil-moisture.md).
BASELINE_MW = 0.166   # unit 1: pristine, indoor, never watered on
BOOST_EFF = 0.85      # assumed boost efficiency, for referring leaks to the 3V3 rail
RAIL_V = 3.3
CELL_MAH = 2500       # usable alkaline LR6 down to 0.9 V
WAKE_MAH = 0.158      # complete 2.993 s PPK2 selection: 0.57 C / 3600
SOIL_SCHEDULE = (
    ("Normal Moisture", 12.0),  # 2 h
    ("Almost Dry", 24.0),       # 1 h
    ("Dry", 6.0),               # 4 h
)


def cmd_diag(args):
    """Power a node, wait for it to fall asleep, then grade its standby leakage."""
    ppk = open_ppk(args.port)
    ppk.use_source_meter()
    ppk.set_source_voltage(args.volt)

    ppk.start_measuring()
    ppk.toggle_DUT_power("ON")
    print(f"powered at {args.volt} mV - waiting for the node to report and sleep ...")

    NOMINAL_RATE = 100_000
    SLEEP_UA = 20_000      # boot/WiFi runs ~150-250 mA; standby is under ~5 mA
    QUIET_S = 1.0          # how long it must stay quiet to count as asleep

    samples: list[float] = []
    sleep_idx = -1          # -1 = not yet detected
    t_sleep = 0.0
    pending = b""
    t0 = time.time()
    try:
        while True:
            chunk, pending = read_samples(ppk, pending)
            if chunk:
                samples.extend(chunk)
            now = time.time() - t0

            if sleep_idx < 0:
                span = int(NOMINAL_RATE * QUIET_S)
                if len(samples) > span and now > QUIET_S:
                    if statistics.fmean(samples[-span:]) < SLEEP_UA:
                        sleep_idx = len(samples) - span   # quiet stretch started here
                        t_sleep = now - QUIET_S
                        print(f"asleep at {t_sleep:.1f}s - discarding {args.sleep_settle:g}s of "
                              f"cap-droop transient, then sampling {args.seconds:g}s ...")
                if now > args.timeout:
                    ppk.stop_measuring()
                    ppk.toggle_DUT_power("OFF")
                    avg = statistics.fmean(samples[-NOMINAL_RATE:]) if samples else 0
                    print(f"\nNEVER SLEPT after {args.timeout:g}s (last-second avg {avg/1000:.1f} mA)")
                    if avg > 50_000:
                        sys.exit(
                            "Looks like a brownout loop, not a measurement: the boot surge "
                            f"exceeds what the PPK2 can source at {args.volt} mV.\n"
                            "Retry at --volt 1800, or add 470-1000 uF across VOUT/GND."
                        )
                    sys.exit("Node stayed awake. Unplug USB (SOF flash-hold), or check WiFi.")
            elif now - t_sleep >= args.sleep_settle + args.seconds:
                break
            time.sleep(0.01)
    finally:
        ppk.stop_measuring()
        ppk.toggle_DUT_power("OFF")

    if sleep_idx < 0:
        sys.exit("internal error: exited the sample loop without detecting sleep")

    elapsed = time.time() - t0
    rate = len(samples) / elapsed

    # Re-locate sleep entry against the MEASURED rate. The live detector above uses a
    # nominal 100 kSa/s only to decide when to stop; if the stream lagged, that index
    # sits inside the ~200 mA boot and a tenth of a second in there doubles the mean.
    # Block means (not per-sample) because a sleeping node still bursts to ~34 mA.
    blk = max(1, int(rate * 0.1))
    nblk = len(samples) // blk
    busy = [i for i in range(nblk)
            if statistics.fmean(samples[i * blk:(i + 1) * blk]) > SLEEP_UA]
    sleep_i = (busy[-1] + 1) * blk if busy else 0
    t_sleep = sleep_i / rate

    # Skip the post-sleep transient. The 3.3 V output cap is still charged when the node
    # first sleeps, so the boost does not burst yet and the input current reads low; only
    # once the cap droops to the regulation point does bursting reach steady state.
    # Measured 2026-07-27 on unit 4: windows starting at sleep entry gave 351-358 uA,
    # windows skipping the first 20 s gave 490-496 uA, and a long app capture confirmed
    # 496 uA steady. Averaging the transient in understates the leak by ~30%.
    settle_i = sleep_i + int(args.sleep_settle * rate)
    standby = samples[settle_i:]
    if len(standby) < rate:
        sys.exit("less than 1 s of standby captured - raise --seconds or lower --sleep-settle")
    standby_ua = statistics.fmean(standby)
    standby_mw = standby_ua * args.volt / 1e6
    ratio = standby_mw / args.baseline

    # Wake cost: everything before sleep, minus what standby would have drawn anyway.
    boot_uC = sum(samples[:sleep_i]) / rate - standby_ua * t_sleep
    boot_mah = max(boot_uC, 0) / 3.6e6

    def life(mw, wakes):
        ua = mw / args.volt * 1e6
        return CELL_MAH / (ua * 24 / 1000 + WAKE_MAH * wakes)

    excess_mw = standby_mw - args.baseline
    print(f"\n=== {args.label or 'unit'} @ {args.volt} mV ===")
    print(f"slept after   : {t_sleep:.1f} s")
    print(f"wake cost     : {boot_mah:.3f} mAh captured")
    print(f"life wake ref : {WAKE_MAH:.3f} mAh (complete 0.57 C boot selection)")
    standby_med = statistics.median(standby)
    print(f"standby       : {standby_ua:.0f} uA mean / {standby_med:.0f} uA median  "
          f"({standby_mw:.3f} mW)")
    print(f"baseline      : {args.baseline / args.volt * 1e6:.0f} uA   ({args.baseline:.3f} mW)")
    print(f"ratio         : {ratio:.1f}x baseline")
    if excess_mw > 0.02:
        leak_ma = excess_mw * BOOST_EFF / RAIL_V
        print(f"excess leak   : ~{leak_ma * 1000:.0f} uA on the {RAIL_V} V rail  "
              f"(~{RAIL_V / leak_ma:.1f} kOhm)")
    print(f"battery life  : state-dependent ({CELL_MAH} mAh AA)")
    for state, wakes in SOIL_SCHEDULE:
        print(f"  {state:15}: {life(standby_mw, wakes):.0f} days  "
              f"(vs {life(args.baseline, wakes):.0f} healthy; {wakes:g} wakes/day)")
    if args.wakes is not None:
        print(f"  custom         : {life(standby_mw, args.wakes):.0f} days  "
              f"(vs {life(args.baseline, args.wakes):.0f} healthy; "
              f"{args.wakes:g} wakes/day)")

    if ratio < 1.25:
        verdict, advice = "HEALTHY", "Standby is at the pristine baseline. Nothing to do."
    elif ratio < 2:
        verdict, advice = (
            "ACCEPTABLE",
            "Standby is elevated but usable; battery life is reduced. "
            "Re-test after 24-48 h at ambient humidity.",
        )
    elif ratio < 4:
        verdict, advice = "MARGINAL", "Slightly elevated. Re-measure after humidity exposure before trusting it."
    elif ratio < 10:
        verdict, advice = "DAMAGED", "Clean the carrier (99%+ IPA flush or ultrasonic, dry warm), then re-measure."
    else:
        verdict, advice = "SEVERELY DAMAGED", "Deep-clean and re-measure, or retire the carrier."
    print(f"\nVERDICT: {verdict}\n{advice}")

    if t_sleep > 8.0:
        print(f"\nWARNING: abnormally long wake ({t_sleep:.1f}s vs the usual ~3s) - a slow or "
              f"failed WiFi association ran into the 30s run_duration cap, so this wake cost "
              f"{boot_mah:.3f} mAh instead of ~0.13. The battery-life figures above assume every "
              f"wake costs the {WAKE_MAH:.3f} mAh fleet reference, so they do not model this "
              "abnormal wake. Re-run to verify normal operation. The standby reading itself is "
              "unaffected.")

    if standby_ua > 5000:
        print("\nNote: standby above 5 mA can also mean the node never truly slept - "
              "confirm USB is unplugged.")
    # Stability checks. A contamination leak is bursty and drifts as the board takes up
    # or loses moisture, so a single short window can land anywhere - say so rather than
    # letting the verdict look more certain than it is.
    half = len(standby) // 2
    h1, h2 = statistics.fmean(standby[:half]), statistics.fmean(standby[half:])
    if max(h1, h2) > 1.2 * min(h1, h2):
        print(f"\nWARNING: unstable - first half {h1:.0f} uA vs second half {h2:.0f} uA "
              f"({max(h1,h2)/min(h1,h2):.1f}x). The leak is drifting; re-run a few times and "
              "use --seconds 60, or let the board settle before trusting the verdict.")
    if standby_ua > 1.5 * standby_med:
        print(f"\nnote: bursty - mean {standby_ua:.0f} uA is well above median {standby_med:.0f} uA. "
              "Expected: the TPS61021 runs in PFM burst mode at light load. Do NOT read burst "
              "rate as a proxy for the leak - packet size changes with load too, so a leaky "
              "unit can show fewer, bigger pulses than a healthy one (measured 2026-07-27: "
              "unit 1 at 96 uA is a continuous 50-180 uA ripple with no discrete bursts, while "
              "unit 4 at 431 uA idles near zero and fires isolated 4.7 mA pulses). Mean is the "
              "only number to compare. Short windows scatter because the first seconds after "
              "sleep entry are a quiet transient - the output cap is still charged - before "
              "bursting reaches steady state.")

    # Attribute out-of-range samples to a window: during boot they are inrush
    # saturation and expected; during standby they would corrupt the verdict.
    boot_over = sum(1 for v in samples[:sleep_i] if v > PPK2_MAX_UA)
    if boot_over:
        print(f"\nnote: {boot_over} boot sample(s) over the 1 A range (inrush saturation; "
              "wake-cost figure is a slight over-estimate)")
    for n in sanity_note(standby, rate):
        print(f"\nWARNING (standby window): {n}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", default="COM20", help="PPK2 data COM port (default COM20)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="dump device metadata").set_defaults(func=cmd_info)

    d = sub.add_parser("diag", help="quick pass/fail: is this unit's standby leaking?")
    d.add_argument("--volt", type=int, default=1800, help="source mV (default 1800; see the 1.5 V gotcha above)")
    d.add_argument("--seconds", type=float, default=30.0,
                   help="standby sampling window, measured AFTER --sleep-settle. Default 30.")
    d.add_argument("--sleep-settle", type=float, default=20.0,
                   help="seconds to discard after sleep entry before measuring. The output cap "
                        "is still charged then, so the boost has not started bursting and the "
                        "reading is ~30%% low. Default 20 (measured: 351 uA including the "
                        "transient vs 496 uA steady state).")
    d.add_argument("--timeout", type=float, default=45.0, help="give up if it never sleeps (run_duration cap is 30 s)")
    d.add_argument("--baseline", type=float, default=BASELINE_MW, help="healthy standby power in mW")
    d.add_argument("--wakes", type=float,
                   help="also print a custom wakes/day estimate; the 2h/1h/4h "
                        "state schedule is always reported")
    d.add_argument("--label", help="name to print in the report, e.g. 'plant 4'")
    d.set_defaults(func=cmd_diag)

    m = sub.add_parser("measure", help="measure current")
    m.add_argument("--mode", choices=["source", "ampere"], default="source")
    m.add_argument(
        "--volt",
        type=int,
        default=1500,
        help="source voltage in mV (source mode). ~1500 = fresh alkaline AA at the "
        "holder terminals. Do NOT use 3.3/3.7 V here: that is the boost input.",
    )
    m.add_argument("--seconds", type=float, default=30.0)
    m.add_argument("--csv", help="write per-sample CSV here")
    m.add_argument(
        "--settle",
        type=float,
        default=0.0,
        help="exclude the first N seconds from the summary (still logged to CSV). "
        "Use ~10 to skip the boot+report burst and read the standby floor.",
    )
    m.add_argument(
        "--keep-power",
        action="store_true",
        help="leave DUT power ON at exit, so a follow-up run can sample standby "
        "without power-cycling (which would force another boot+report)",
    )
    m.add_argument(
        "--assume-powered",
        action="store_true",
        help="do not switch DUT power on. NOTE: the PPK2 drops its DUT output when "
        "the serial session reopens, so this does NOT carry power across separate "
        "runs -- it only helps when the node is fed from somewhere else. Chaining "
        "runs with --keep-power then --assume-powered just measures an unpowered "
        "rig (~0.2 uA noise floor).",
    )
    m.set_defaults(func=cmd_measure)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
