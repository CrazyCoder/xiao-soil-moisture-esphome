# XIAO Soil Moisture Sensor — low-power ESPHome firmware

Replacement ESPHome firmware for the [Seeed XIAO Soil
Sensor](https://www.seeedstudio.com/XIAO-Soil-Sensor-p-6452.html). It reports
soil moisture to Home Assistant over MQTT, then returns to deep sleep.

The kit ships with firmware that works. This firmware replaces it to solve one
problem: battery life.

| | Stock firmware | This firmware |
|---|---|---|
| Awake time per wake | `run_duration: 120s` | **~2.8 s** |
| Transport | native API | MQTT, retained topics |
| Address | DHCP | static IP + `fast_connect` |
| Sleep schedule | 8 h / 1 h / 15 min | 2 h / 1 h / 4 h |
| Battery percentage | linear 1.2–1.5 V, every 5 s | alkaline curve, sampled before WiFi |
| Wake energy | — | **0.158 mAh**, measured |
| Standby current | — | **92 µA**, measured |
| Life on one AA | about 1 month, observed | **about 600 days** at the Normal interval |

The awake time controls the battery life. The stock firmware holds the radio on
for 120 seconds at every wake. It does this even after the report is complete.
This is the main cause of the short battery life.

**About the one-month figure.** That is what this deployment observed with the
factory configuration. It is not a vendor specification. Seeed publish no
runtime figure for the product. The cause is visible in their published YAML:
`run_duration: 120s`.

Every power number here is measured with a hardware power analyzer, and the
tools to repeat the measurement are in this repository. Refer to
[docs/power.md](docs/power.md).

## Contents

| Path | Purpose |
|---|---|
| `esphome/` | The firmware. This directory is the ESPHome configuration root. |
| `esphome/packages/` | Shared base configuration and C++ helpers |
| `esphome/sample-with-button.yaml` | Device sample for a board with the CN4 button |
| `esphome/sample-timer-only.yaml` | Device sample for a board with CN4 removed |
| `tools/soil-ota-babysitter.py` | Unattended OTA updates for devices that sleep |
| `tools/ppk2-monitor.py`, `tools/analyze-capture.py` | Power measurement |
| `homeassistant/soil-alerts.yaml` | Sample alerts, not tied to any notify service |
| `docs/power.md` | Measurement method, evidence, and how to repeat it |
| `docs/hardware-notes.md` | A hardware defect that empties batteries |

## Hardware

| Part | Link |
|---|---|
| XIAO Soil Sensor (carrier and probe) | https://www.seeedstudio.com/XIAO-Soil-Sensor-p-6452.html |
| Product wiki, with the factory YAML under Resources | https://wiki.seeedstudio.com/xiao_soil_moisture_sensor/ |
| Seeed Studio XIAO ESP32-C6 | https://www.seeedstudio.com/Seeed-Studio-XIAO-ESP32C6-p-5884.html |
| XIAO ESP32-C6 wiki | https://wiki.seeedstudio.com/xiao_esp32c6_getting_started/ |

You supply one alkaline AA (LR6) cell. The carrier has a boost converter, which
keeps the device alive down to about 0.9 V.

**The external u.FL antenna is already attached. Keep it connected.** Each
failed association keeps the radio on, and radio time is the whole power
budget. If you remove the antenna, set `external_antenna: "false"` in your
device file. The firmware then selects the built-in ceramic antenna. A radio
that drives an unconnected u.FL connector is the worst of the three states.

## Prerequisite: MQTT

The stock firmware uses the ESPHome native API, which needs no setup. This
firmware needs an MQTT broker. Many Home Assistant users do not have one yet,
so this section explains why, and how to add it.

### Why MQTT is necessary

The native API is a connection that Home Assistant opens **to the device** and
holds open. That model expects a device which is always reachable. This device
sleeps for more than 99.9% of the time, which breaks the model in three ways:

- **Entities become unavailable.** Home Assistant cannot reach a device that
  sleeps, so it marks the device offline between reports.
- **Each wake pays for a handshake.** The connection, the encryption and the
  entity subscriptions must complete before any data moves. This is pure
  overhead in a 2.8 second budget.
- **The timing must agree.** The device is present for a moment. Anything that
  does not listen at that moment misses the report.

MQTT reverses the direction. The device connects to the broker, publishes, and
sleeps. A **retained** message stays on the broker, so Home Assistant keeps the
last reading on the dashboard. Nothing must listen at the moment of the report.

### How to set it up

1. Install the **Mosquitto broker** add-on. The [Home Assistant MQTT
   documentation](https://www.home-assistant.io/integrations/mqtt/) recommends
   it, and Home Assistant generates the credentials for you.
2. Add the **MQTT integration**. Discovery is on by default. Discovery creates
   all the entities of this firmware without more configuration.
3. Put the broker address, the user name and the password into `secrets.yaml`.

Refer also to the [ESPHome MQTT client
documentation](https://esphome.io/components/mqtt/).

### Settings that look wrong, but are correct

The `mqtt:` block in the base package is tuned for deep sleep:

- `birth_message`, `will_message` and `shutdown_message` are **empty**, which
  disables them. A will message announces "offline" at every deep sleep. That
  reintroduces the exact problem MQTT was chosen to solve.
- `enable_on_boot: false`, and `wifi.on_connect` starts MQTT instead. This skips
  the 5 second retry gate of the MQTT client. That gate alone is longer than the
  whole target wake time.
- `reboot_timeout: 0s`, so a broker outage does not restart the device.
- `on_connect` runs the report script, so the wake is connect, publish, sleep.

> **Trap.** If you enable MQTT and do not use the native API, you must remove
> `api:` or set `api.reboot_timeout: 0s`. If you do not, the device restarts
> every 15 minutes, because no client connects to the API. This configuration
> has no `api:` block, so it is already correct. On a battery device a
> 15 minute restart cycle empties the cell.

## Quick start

```bash
git clone https://github.com/CrazyCoder/xiao-soil-moisture-esphome.git
cd xiao-soil-moisture-esphome/esphome
cp secrets.yaml.example secrets.yaml
```

Edit `secrets.yaml` and enter your WiFi and MQTT values. Then copy a sample for
your first plant:

```bash
cp sample-with-button.yaml plant-1.yaml
```

Edit `name`, `friendly_name` and `static_ip` in that file. Give every device a
different name and a different address. Then connect the board over USB and
flash it:

```bash
# Linux / macOS
esphome run plant-1.yaml --device /dev/ttyACM0
```

```powershell
# Windows (PowerShell). Read "Windows notes" below first.
esphome run plant-1.yaml --device COM11
```

Wait for the green LED before you flash. The LED means the device is awake and
its port is present. Hold BOOT while you connect the cable only if no port
appears, because a device in deep sleep does not enumerate its USB interface.

Later updates go over the network. Refer to [How to update the
firmware](#how-to-update-the-firmware).

Use `sample-timer-only.yaml` instead if the CN4 button is absent from your
board.

### Windows notes

**Build from PowerShell or the Command Prompt.** If you do that, the rest of this
section does not affect you.

The trap below applies only to a shell of the MSYS family: Git Bash, an MSYS2
terminal, or any terminal that puts `Git\usr\bin` on the `PATH`. Some editors
and agent tools also use Git Bash as their default shell.

ESP-IDF refuses to build under MSYS or MinGW. If `MSYSTEM` is set, or if
`Git\usr\bin` is on the `PATH`, ESP-IDF detects an MSYS environment and skips
the link step. **ESPHome then still prints `Successfully compiled program` and
exits 0.** It produces no binary. A later `upload` flashes an old binary, or it
fails for a reason that makes no sense.

The warning appears earlier in the output:

```
MSys/Mingw is no longer supported. Please follow the getting started guide ...
WARNING flasher_args.json not found, cannot create factory.bin
WARNING Firmware not found: ...
```

If you see `Firmware not found` or `ELF not found`, the build failed. Do not
trust the success line above it.

To find your COM port:

```powershell
Get-CimInstance Win32_SerialPort | Select-Object DeviceID, Description
```

The XIAO ESP32-C6 uses the native USB-Serial-JTAG interface. It appears as
`COMn` on Windows, and as `/dev/ttyACM0` on Linux.

## Power design

The levers, in order of effect.

1. **Short awake time.** The device reports and then sleeps, instead of a fixed
   `run_duration`. This is the 40x lever. Everything below is smaller.
2. **MQTT, not the native API.** Retained topics hold the last value while the
   device sleeps. There is no handshake and no subscription setup at each wake.
   MQTT starts from `wifi.on_connect`, which skips a 5 second retry gate.
3. **A static IP with `fast_connect`.** DHCP costs seconds of radio time at
   every wake.
4. **The supplied u.FL antenna.** Keep it connected. Fewer retries means a
   shorter wake.
5. **Sleep intervals that follow the moisture state.** Refer to [Sleep
   schedule](#sleep-schedule).
6. **The probe drive stops immediately after the sample.** It also stops during
   a USB session and an OTA hold.
7. **A battery percentage from an alkaline curve**, sampled before WiFi starts.
   Refer to [The battery percentage](#the-battery-percentage).
8. **A rewake guard.** Water on the button is a power problem, because the
   button is also the wake pin. A wet button woke one device continuously and
   emptied a full cell in about two hours. After 5 button wakes in a row the
   firmware takes a timer-only sleep.

![One wake, measured with a PPK2](docs/img/ppk2-wake.png)

One wake: 2.926 seconds, 199.19 mA average, 0.58 C. The 1.63 A peak is the
radio start.

## Sleep schedule

The interval chosen from the **current** reading controls how fast the **next**
change is found. That is the logic behind these defaults.

| Current state | Sleep | Wakes/day | What it buys | Life on 2500 mAh |
|---|---:|---:|---|---:|
| Normal Moisture | 2 h | 12 | Find Almost Dry within 2 h | ~608 days |
| Almost Dry | 1 h | 24 | Find Dry within 1 h | ~416 days |
| Dry | 4 h | 6 | Confirm watering within 4 h | ~791 days |

A plant is watered at most once a day, and Home Assistant keeps the Dry state.
A short Dry interval therefore only confirms the recovery faster. It does not
find the entry into Dry any sooner. The budget belongs on the Normal interval,
where it halves the early warning time.

**These values are one household's compromise. Change them for your plants.**
Set them in your device file:

```yaml
substitutions:
  sleep_normal_ms:     "7200000"    #  2 h
  sleep_almost_dry_ms: "3600000"    #  1 h
  sleep_dry_ms:       "14400000"    #  4 h
```

Calculate the cost first: `wakes per day = 86400 / (interval_ms / 1000)`. Each
wake costs 0.158 mAh. The stock 15 minute Dry interval is 96 wakes each day,
and it spends the charge after the alert has already gone out.

### Why the device has no clock

The device does not know the time of day. This is deliberate.

A clock needs a time source and a synchronisation at each wake. Both cost awake
time, and awake time is the whole power budget. The device must also hold the
time across deep sleep.

The gain is small. A plant does not dry to a schedule, and the interval logic
above already reacts to the measurement itself. A fixed 2.8 second wake with no
clock is a better trade than a shorter interval at night.

### What is planned

Neither of these is in the firmware today. A change to an interval needs a new
build and an OTA update.

**1. Remote interval settings.** A retained MQTT command will set the three
fallback intervals with no new build:

```sh
mosquitto_pub ... -r -t 'plant-1/sleep_schedule/command' -m 'SET 7200 3600 14400'
```

It follows the pattern of the calibration command below. The device validates
all three values together, rejects a bad command, and clears the retained
command itself.

**2. A sleep time from Home Assistant.** Home Assistant holds the full history
of each plant, with accurate timestamps. It can estimate how fast each pot
dries. It can therefore choose a better next sleep than a fixed table can, and
it can do that per plant.

The device would publish its reading, wait a short time for a live MQTT answer,
and accept one bounded duration for that wake only. **This is a message, not a
firmware update.**

Safety rules for that design:

- The answer is **not retained**. A stale duration cannot replay after a
  restart of the broker, the device or Home Assistant.
- The device clamps the value to a safe minimum and maximum. An accidental one
  second interval cannot empty the cell.
- On a timeout, a bad value, or no answer, the device uses its own saved
  schedule.
- The device publishes the applied duration and the reason for it, so you can
  audit the decision.

The device therefore stays autonomous. Home Assistant only advises it.

## The battery percentage

The stock firmware maps the voltage to a percentage with a straight line:

```cpp
if (x < 1.2)      { return 0.0; }
else if (x > 1.5) { return 100.0; }
else              { return ((x - 1.2) / (1.5 - 1.2)) * 100.0; }
```

This has two faults:

1. **An alkaline cell does not discharge in a straight line.** A new cell holds
   100% for several weeks. It then crosses the whole scale in about twelve days.
2. **It calls 1.2 V empty.** At 1.2 V under this load, about 24% of the usable
   service remains. The boost converter also runs down to 0.9 V. The stock map
   reports a dead battery while a quarter of the charge is still available.

This firmware publishes the raw voltage and calculates the percentage from a
lookup table:

| Cell voltage | 1.60 | 1.50 | 1.40 | 1.30 | 1.20 | 1.10 | 1.00 | 0.90 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Reported % | 100 | 96 | 83 | 57 | 24 | 11 | 3 | 0 |

The table comes from the **Duracell Basic AA (MN1500 / LR6)** datasheet,
document `MN15EUBS0919`, and its **50 mW** constant-power curve. That is the
lightest curve Duracell publish for an AA cell, and this device averages about
0.26 mW. The better known Coppertop datasheet stops at 250 mW, which is almost
1000 times the real load. Refer to [docs/power.md](docs/power.md#which-curve-and-why)
for the chart and the full explanation.

The firmware samples the voltage once at boot priority 599. That point is after
the ADC is ready, but before WiFi starts, and after hours of sleep. This removes
the ±10% swings that the radio load caused.

The percentage is an estimate, not a fuel gauge. Use it for alerts. Use the raw
voltage to diagnose one cell. Refer to [docs/power.md](docs/power.md).

## Calibration

Calibration sets the dry voltage and the wet voltage of your probe. Both values
are kept in the flash memory of the device.

**With the button.** Press the button 3 times quickly. The red phase starts:
hold the probe in dry soil for 10 seconds. The green phase then starts: hold the
probe in the soil immediately after you water it, for 10 seconds. The firmware
checks both values before it saves them, so a bad second phase cannot overwrite
a good pair.

**Over MQTT.** This also works on a board with no button:

```sh
# Restore the configured seed values:
mosquitto_pub -h BROKER -u USER -P PASS \
  -r -t 'plant-1/calibration/command' -m RESET

# Apply your own measured values. Dry must be at least 0.150 V above wet:
mosquitto_pub -h BROKER -u USER -P PASS \
  -r -t 'plant-1/calibration/command' -m 'SET 2.750 1.200'
```

The command is retained, so the device receives it at its next wake. The device
checks the command, saves the values, takes a fresh reading, publishes the
result, and then clears the retained command itself. A bad command is rejected
and the stored values do not change.

`RESET` restores the seed values. It is not a substitute for a real
per-probe calibration.

## Home Assistant

MQTT discovery creates these entities for each device:

| Entity | Purpose |
|---|---|
| Soil Moisture Status | Dry, Almost Dry, or Normal Moisture |
| Soil moisture (raw) | Probe voltage. Higher is drier. |
| Battery | Percentage from the alkaline curve |
| Battery voltage | Raw cell voltage. Use this to diagnose a cell. |
| Soil sensor fault | Problem class. On for a bad ADC or bad calibration. |
| Button wake count | Consecutive button wakes. Above 2 means a wet button. |
| WiFi signal | Signal strength in dBm |
| MCU temperature | Die temperature, sampled before WiFi starts |
| Calibration dry / wet / span | The stored calibration |
| Calibration command result | Result of the last calibration attempt |
| Next sleep | The interval chosen at this wake |
| Wake reason / Reset reason | Timer, button, or cold boot |
| Firmware build | Exact version and configuration hash |

`Calibration command result` stays `unknown` until you calibrate. It reports the
last operation. It is not a health sensor.

Sample alerts are in
[`homeassistant/soil-alerts.yaml`](homeassistant/soil-alerts.yaml): battery low,
button wet or stuck, plant needs water, and sensor silent. They use a
placeholder notify service, so they work with any notification method.

## How to update the firmware

A device that is awake for 2.8 seconds every two hours is not a normal update
target. There are three methods.

### The USB hold

`packages/usb_host_detect.h` reads the USB-Serial-JTAG frame counter twice,
20 ms apart. An enumerated USB host sends SOF frames at 1 kHz, so the counter
moves. A charger or a power bank never sends SOF frames. The check therefore
answers "is a real host attached?", not "is there 5 V?".

When a host appears, the firmware stops the deep sleep and **turns on the green
LED**. The LED means "I am awake, flash me now". When you disconnect the cable,
the device returns to its schedule. Without this hold you must complete a flash
inside a 2.8 second window.

### Method A: USB

This is the simplest method. It is also the only method for the first flash from
the stock firmware.

Connect the cable, wait for the green LED, then flash the device:

```bash
esphome run plant-1.yaml --device /dev/ttyACM0    # Linux / macOS
esphome run plant-1.yaml --device COM11           # Windows
```

Disconnect the cable when the flash is complete. The device then returns to its
schedule. On Windows, read [Windows notes](#windows-notes) first.

**You do not need the BOOT button while the device is awake.** The green LED
means the USB hold is active. The port is then present, and ESPHome puts the
chip into download mode itself.

**Hold BOOT only if no port appears.** A device in deep sleep does not enumerate
its USB interface. There is no port until it wakes. Hold BOOT while you connect
the cable. This starts the ROM bootloader, which always enumerates.

### Method B: manual OTA

Use this for one deployed device.

```bash
# 1. Retained hold. The device stays awake after its next wake.
mosquitto_pub -h BROKER -u USER -P PASS -r -t 'plant-1/ota' -m ON

# 2. Wake it: press the button once, or wait for the next report (up to 4 h).

# 3. Upload once the device answers.
esphome upload plant-1.yaml --device 192.168.1.61

# 4. VERIFY the running build.
mosquitto_sub -h BROKER -u USER -P PASS \
  -t 'plant-1/sensor/firmware_build/state' -C 1 -W 10

# 5. Release the hold. The device sleeps immediately.
mosquitto_pub -h BROKER -u USER -P PASS -r -t 'plant-1/ota' -m OFF
```

> **Step 4 is not optional.** The bootloader reverts an image that is not marked
> valid before the next restart. A deep-sleep device restarts constantly.
> This firmware marks itself valid after one complete report. The message
> "OTA successful" from the uploader only means that the bytes arrived.

> **A retained `ON` that you forget empties the battery**, because it holds the
> device awake. Always do step 5. The firmware clears a hold after 30 minutes as
> a backstop.

### Method C: the babysitter

`tools/soil-ota-babysitter.py` does method B for several devices at the same
time. For each device it does five steps: hold the device awake, wait for the next
natural wake, upload, verify the exact version and configuration hash, then
release the hold. The release is guaranteed through `finally`, signal handlers and
`atexit`, because a forgotten hold costs a battery.

The script compiles every selected device **before** it sets any hold. A failed
build therefore leaves every device that sleeps untouched.

Setup on a new Linux system:

```bash
# 1. Prerequisites (Debian or Ubuntu)
sudo apt install -y git tmux
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Configuration and secrets
git clone https://github.com/CrazyCoder/xiao-soil-moisture-esphome.git
cd xiao-soil-moisture-esphome/esphome
cp secrets.yaml.example secrets.yaml
$EDITOR secrets.yaml

# 3. ESPHome itself. The script calls it.
uv tool install esphome

# 4. Run it under tmux. A run can wait hours for a device to wake.
tmux new -s soil-ota \
  'ESPHOME_CONFIG_DIR=$PWD uv run ../tools/soil-ota-babysitter.py plant-1 plant-2 2>&1 | tee -a ~/soil-ota.log'
```

The script finds the devices itself. It reads the `name` and `static_ip` values
from every device file in the configuration directory. Select devices by name
(`plant-1`), by number (`1`), or with `all`.

Two requirements: the machine must reach both the MQTT broker and the addresses
of the devices, so run it on the same network. The wake timeout is 6 hours,
against a longest schedule of 4 hours.

The script publishes each result to `soil-ota-babysitter/notification`. Home
Assistant can forward that topic to any notification service.

## Verify the power numbers

The measurements use a [Nordic Power Profiler Kit II
(PPK2)](https://www.nordicsemi.com/Products/Development-hardware/Power-Profiler-Kit-2),
a USB power analyzer. It supplies the board and measures from 200 nA to 1 A at
100,000 samples per second. **You do not need one to use this firmware.** It is
only for a measurement or a diagnosis.

A pass or fail check takes about 55 seconds:

```bash
uv run tools/ppk2-monitor.py diag --label "plant 1"
```

Refer to [docs/power.md](docs/power.md) for the connections, the method, the
full results and the cautions.

## A hardware defect that empties batteries

Some units lose a battery in weeks. The cause is not the firmware. On the
carrier board the BAT+ pad is 0.54 mm from the GPIO2 wake pin at the CN4
button. Water reaches the button through the enclosure.

The leakage resistance decides the symptom. A low resistance makes the switch
look pressed, so the device wakes again at every sleep. A higher resistance
gives no false wakes, but a constant standby current empties the cell.

**The damage is often invisible**, because water also gets inside the switch
body. A board that looks perfect can still leak. Measure it, do not inspect it.

The repair is to remove CN4. This returned three damaged units from 6.3x, 12.2x
and 22.8x the baseline standby current back to 88 µA, 92 µA and 163 µA.

Refer to [docs/hardware-notes.md](docs/hardware-notes.md) for the photographs,
the detection method and the repair.

## Notes and traps

- **`discovery_unique_id_generator: mac` is necessary.** The ESPHome default is
  `legacy`, which gives the same discovery id to every device. Home Assistant
  then ignores or cross-wires every device after the first one.
- **Verify a build from MQTT, not from the uploader.** Read
  `<node>/sensor/firmware_build/state`.
- **A network failure is safe.** The configuration has three settings that look
  wrong together:

  | Setting | Value | Purpose |
  |---|---|---|
  | `deep_sleep.run_duration` | **30 s** | The real safety limit on awake time |
  | `wifi.reboot_timeout` | `0s` | No restart when WiFi fails |
  | `mqtt.reboot_timeout` | `0s` | No restart when the broker fails |

  On a battery device a restart is the dangerous answer and sleep is the safe
  one. A restart brings the radio back up to try again, at about 113 mA. The
  30 second limit instead forces a sleep, and the device retries hours later.

  With the 30 second limit, a failed wake costs about 0.94 mAh. At 12 wakes each
  day the cell survives for months with WiFi switched off. A restart loop empties
  the same cell in about 22 hours.

  A failure is therefore silent by design. The `Soil - sensor silent` sample
  alert makes it visible.

## Firmware versions

The version appears in the `sw_version` of the Home Assistant device, and on the
`Firmware build` entity with the exact configuration hash.

| Version | Change |
|---|---|
| 1.0 | Adaptive schedule, alkaline curve, voltage sampled before WiFi |
| 1.1.0 | MCU temperature |
| 1.2.0 | Remote calibration, exact build id, fault handling, diagnostics |
| 1.2.1 | Probe drive stops after 5 s when MQTT never connects |
| 1.2.2 | Publishes the healthy initial fault state instead of `unknown` |
| 1.2.3 | Sleep intervals and antenna choice became substitutions |

Versions follow SemVer. A patch fixes a fault with no change to behaviour or
entities. A minor version adds behaviour or entities.

## Credits and licence

MIT. Refer to [LICENSE](LICENSE).

The pin map and the PWM probe drive come from the factory ESPHome configuration
of Seeed Studio. That file is in the Resources section of the [product
wiki](https://wiki.seeedstudio.com/xiao_soil_moisture_sensor/). The rest of the
firmware is a rewrite.
