#!/usr/bin/env python
# /// script
# requires-python = ">=3.10"
# dependencies = ["paho-mqtt>=1.6", "PyYAML>=6.0"]
# ///
"""Babysit OTA updates for sleeping XIAO soil-moisture sensors.

For each device: set retained <node>/ota = ON (device stays awake after its
next scheduled wake), wait for a live MQTT publish from it (= awake), upload
the binary compiled by this run, and verify the exact application version plus
ESPHome config hash reported by the new firmware. The OFF publish is guaranteed
via finally + signal handlers + atexit - a leaked retained ON would hold a
device awake and drain its battery. While any selected device is pending, the
script retains soil-ota-babysitter/active = ON so Home Assistant can suppress
the expected long-hold alarm; the same cleanup paths retain OFF at completion.

Devices are discovered by parsing the ESPHome device stubs in the config
directory, so this script has no per-fleet configuration. Select devices by node
name, by numeric suffix, or with "all".

Run under tmux - a run waits for each device's next natural wake, up to 6 hours:
  tmux new -s soil-ota \
    'uv run soil-ota-babysitter.py 2 3 2>&1 | tee -a ~/soil-ota.log'

The config directory defaults to this script's own directory. Override it with
--config-dir or the ESPHOME_CONFIG_DIR environment variable.
"""

import argparse
import atexit
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml
import paho.mqtt.client as mqtt
import paho.mqtt.publish as mqtt_publish

CONFIG_DIR = Path(os.environ.get("ESPHOME_CONFIG_DIR") or Path(__file__).resolve().parent)
ESPHOME = os.environ.get("ESPHOME_BIN") or shutil.which("esphome") or "esphome"
NOTIFICATION_TOPIC = "soil-ota-babysitter/notification"
ACTIVE_TOPIC = "soil-ota-babysitter/active"
WAKE_TIMEOUT_S = 6 * 3600     # longest scheduled report cadence is 4h (dry)
VERIFY_TIMEOUT_S = 300        # post-flash reboot -> exact Firmware build report
UPLOAD_ATTEMPTS = 3
COMPILE_TIMEOUT_S = 30 * 60

# Loaded in main(), after argument parsing, so --help works with no config.
secrets: dict = {}

_log_lock = threading.Lock()
_active = False


def log(msg: str) -> None:
    with _log_lock:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def notify(msg: str) -> None:
    """Queue a Telegram notification through Home Assistant's MQTT bridge."""
    payload = json.dumps(
        {"title": "🌱 Soil OTA babysitter", "message": msg},
        ensure_ascii=False,
    )
    try:
        if client.is_connected():
            info = client.publish(
                NOTIFICATION_TOPIC,
                payload,
                qos=1,
                retain=False,
            )
            info.wait_for_publish(10)
            if (
                info.rc != mqtt.MQTT_ERR_SUCCESS
                or not info.is_published()
            ):
                raise RuntimeError(f"publish incomplete (rc={info.rc})")
        else:
            # Compilation happens before the long-lived MQTT client connects.
            # A one-shot publish preserves failure notifications without
            # publishing an OTA hold or changing device state.
            mqtt_publish.single(
                NOTIFICATION_TOPIC,
                payload=payload,
                qos=1,
                retain=False,
                hostname=secrets["mqtt_broker"],
                port=1883,
                auth={
                    "username": secrets["mqtt_username"],
                    "password": secrets["mqtt_password"],
                },
            )
        log("Home Assistant notification queued through MQTT")
    except Exception as e:  # notification is best-effort
        log(f"Home Assistant MQTT notification failed: {e}")


class Device:
    def __init__(self, node: str, ip: str, yaml_name: str):
        self.node = node
        self.ip = ip
        self.yaml = yaml_name
        self.awake = threading.Event()
        self.build_verified = threading.Event()
        self.expected_build = ""
        self.result = "pending"
        self.off_sent = False

    def __str__(self) -> str:
        return self.node


class _StubLoader(yaml.SafeLoader):
    """SafeLoader that tolerates ESPHome's custom tags.

    Device stubs use `!include`, which SafeLoader rejects. Treat any `!tag` as
    opaque and return its raw value, so `!include packages/x.yaml` becomes the
    string "packages/x.yaml".
    """


def _opaque_tag(loader, tag_suffix, node):  # noqa: ARG001 - signature fixed by PyYAML
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_StubLoader.add_multi_constructor("!", _opaque_tag)


def discover_devices(config_dir: Path) -> dict[str, Device]:
    """Find every device stub that includes the soil-moisture base package.

    A stub is a YAML file whose `packages:` includes the base package, and whose
    `substitutions:` supply `name` and `static_ip`. Returns {node_name: Device}.
    """
    found: dict[str, Device] = {}
    for path in sorted(config_dir.glob("*.yaml")):
        try:
            doc = yaml.load(path.read_text(encoding="utf-8"), Loader=_StubLoader) or {}
        except (yaml.YAMLError, UnicodeDecodeError, OSError):
            continue          # not a parseable config file
        if not isinstance(doc, dict):
            continue
        packages = doc.get("packages") or {}
        if not isinstance(packages, dict):
            continue
        if not any(
            "xiao-soil-moisture-monitor.base.yaml" in str(v) for v in packages.values()
        ):
            continue
        subs = doc.get("substitutions") or {}
        node, ip = subs.get("name"), subs.get("static_ip")
        if not node or not ip:
            log(f"skipping {path.name}: no name/static_ip substitution")
            continue
        found[str(node)] = Device(str(node), str(ip), path.name)
    return found


devices: list[Device] = []

try:  # paho-mqtt 2.x
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION1,
        client_id=f"soil-ota-babysitter-{os.getpid()}",
    )
except AttributeError:  # paho-mqtt 1.x
    client = mqtt.Client(client_id=f"soil-ota-babysitter-{os.getpid()}")
# Credentials are applied in main(), once secrets.yaml has been loaded.


def output_tail(proc: subprocess.CompletedProcess, lines: int = 20) -> str:
    output = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    return "\n".join(output.strip().splitlines()[-lines:]) or "(no output)"


def compile_device(d: Device) -> None:
    """Compile current source and record the exact identity to verify later."""
    log(f"[{d.node}] compiling {d.yaml}")
    proc = subprocess.run(
        [str(ESPHOME), "compile", d.yaml],
        cwd=CONFIG_DIR,
        capture_output=True,
        text=True,
        timeout=COMPILE_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{d.node} compile failed:\n{output_tail(proc)}")

    build_dir = CONFIG_DIR / ".esphome" / "build" / d.node
    build_info_path = build_dir / "build_info.json"
    defines_path = build_dir / "src" / "esphome" / "core" / "defines.h"
    firmware_path = build_dir / "build" / "firmware.ota.bin"
    missing = [
        str(path)
        for path in (build_info_path, defines_path, firmware_path)
        if not path.is_file()
    ]
    if missing:
        raise RuntimeError(
            f"{d.node} compile returned success but artifacts are missing: "
            + ", ".join(missing)
        )

    build_info = json.loads(build_info_path.read_text())
    defines = defines_path.read_text()
    version_match = re.search(
        r'^#define ESPHOME_PROJECT_VERSION "([^"]+)"$', defines, re.MULTILINE
    )
    if version_match is None:
        raise RuntimeError(f"{d.node} has no ESPHOME_PROJECT_VERSION")

    version = version_match.group(1)
    config_hash = int(build_info["config_hash"])
    d.expected_build = f"{version} / 0x{config_hash:08x}"
    log(
        f"[{d.node}] compiled {d.expected_build} "
        f"(ESPHome {build_info.get('esphome_version', '?')})"
    )


def on_message(_client, _userdata, msg):
    if msg.retain:  # stale replayed state, not proof of life
        return
    for d in devices:
        if not msg.topic.startswith(d.node + "/"):
            continue
        if msg.topic == f"{d.node}/ota":  # echo of our own hold-flag publish
            return
        # ESPHome text sensors use the MQTT /sensor/ topic segment (the
        # Home Assistant entity is also a sensor), not /text_sensor/.
        if msg.topic == f"{d.node}/sensor/firmware_build/state":
            reported_build = msg.payload.decode(errors="replace")
            if reported_build == d.expected_build:
                d.build_verified.set()
                log(f"[{d.node}] exact build reported: {reported_build}")
            else:
                log(
                    f"[{d.node}] build mismatch: expected {d.expected_build}, "
                    f"reported {reported_build}"
                )
        if not d.awake.is_set():
            log(f"[{d.node}] awake ({msg.topic})")
        d.awake.set()
        return


def set_hold(d: Device, on: bool) -> None:
    client.publish(f"{d.node}/ota", "ON" if on else "OFF", retain=True).wait_for_publish(10)
    if not on:
        d.off_sent = True
    log(f"[{d.node}] ota hold {'ON' if on else 'OFF'}")


def set_active(on: bool) -> None:
    """Publish the retained maintenance state used to suppress expected alarms."""
    global _active
    info = client.publish(
        ACTIVE_TOPIC,
        "ON" if on else "OFF",
        qos=1,
        retain=True,
    )
    info.wait_for_publish(10)
    if info.rc != mqtt.MQTT_ERR_SUCCESS or not info.is_published():
        raise RuntimeError(f"maintenance-state publish incomplete (rc={info.rc})")
    _active = on
    log(f"babysitter maintenance {'ON' if on else 'OFF'}")


def release_all(*_args):
    for d in devices:
        if not d.off_sent:
            try:
                set_hold(d, False)
            except Exception as e:
                log(f"[{d.node}] FAILED to release hold: {e} - publish OFF manually!")
    if _active:
        try:
            set_active(False)
        except Exception as e:
            log(f"FAILED to release babysitter maintenance state: {e}")


def babysit(d: Device) -> None:
    try:
        set_hold(d, True)
        uploaded = False
        for wake_round in (1, 2):  # round 2: device raced back to sleep before the hold landed
            if not d.awake.wait(WAKE_TIMEOUT_S):
                d.result = "never woke"
                return
            time.sleep(3)  # let its report cycle finish
            for attempt in range(1, UPLOAD_ATTEMPTS + 1):
                log(f"[{d.node}] wake round {wake_round}, upload attempt {attempt}")
                # Ignore exact-build reports from before this upload attempt.
                # A matching post-upload state proves the intended image booted.
                d.build_verified.clear()
                proc = subprocess.run(
                    [str(ESPHOME), "upload", d.yaml, "--device", d.ip],
                    cwd=CONFIG_DIR, capture_output=True, text=True, timeout=600,
                )
                if proc.returncode == 0:
                    uploaded = True
                    break
                log(f"[{d.node}] upload failed:\n{output_tail(proc, 5)}")
                time.sleep(15)
            if uploaded:
                break
            d.awake.clear()  # wait for the next scheduled wake and retry
        if not uploaded:
            d.result = "upload failed"
            return
        log(f"[{d.node}] upload ok, waiting for exact build {d.expected_build}")
        if d.build_verified.wait(VERIFY_TIMEOUT_S):
            time.sleep(5)  # report cycle also runs mark_successful() right after
            d.result = "updated+verified"
        else:
            d.result = (
                f"uploaded but NOT verified as {d.expected_build} "
                "(check for rollback!)"
            )
    except Exception as e:
        d.result = f"error: {e}"
    finally:
        try:
            set_hold(d, False)
        except Exception as e:
            log(f"[{d.node}] FAILED to release hold: {e} - publish OFF manually!")
        log(f"[{d.node}] done: {d.result}")
        notify(f"Soil OTA {d.node}: {d.result}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Update selected sleeping soil sensors over OTA."
    )
    parser.add_argument(
        "devices",
        metavar="DEVICE",
        nargs="+",
        help='node names (plant-1), numeric suffixes (1), or "all"',
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help="ESPHome config directory "
        "(default: $ESPHOME_CONFIG_DIR, else this script's directory)",
    )
    args = parser.parse_args()

    global CONFIG_DIR
    if args.config_dir:
        CONFIG_DIR = args.config_dir.resolve()

    available = discover_devices(CONFIG_DIR)
    if not available:
        log(f"no soil-moisture device stubs found in {CONFIG_DIR}")
        return 2

    if [t.lower() for t in args.devices] == ["all"]:
        selected = list(available)
    else:
        selected = []
        for token in args.devices:
            match = [n for n in available if n == token or n.endswith(f"-{token}")]
            if len(match) != 1:
                log(
                    f"'{token}' matched {len(match)} devices; "
                    f"available: {', '.join(sorted(available))}"
                )
                return 2
            selected.append(match[0])

    devices.extend(available[n] for n in dict.fromkeys(selected))

    secrets.update(yaml.safe_load((CONFIG_DIR / "secrets.yaml").read_text()))
    client.username_pw_set(secrets["mqtt_username"], secrets["mqtt_password"])
    log(f"config dir {CONFIG_DIR}, esphome {ESPHOME}")

    # Compile sequentially before publishing a single hold. ESP-IDF/PlatformIO
    # builds contend for shared caches, and a failed build must leave every
    # sleeping device untouched.
    try:
        for d in devices:
            compile_device(d)
    except Exception as e:
        log(f"ABORT before OTA holds: {e}")
        notify(f"Soil OTA aborted before holds: {e}")
        return 2

    atexit.register(release_all)
    # SIGHUP does not exist on Windows; the others are portable.
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, lambda *_: sys.exit(1))  # -> atexit -> release_all

    client.on_message = on_message
    client.connect(secrets["mqtt_broker"], 1883, 60)
    for d in devices:
        client.subscribe(f"{d.node}/#")
    client.loop_start()

    set_active(True)
    log(f"babysitting {[d.node for d in devices]}")
    threads = [threading.Thread(target=babysit, args=(d,), daemon=True) for d in devices]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    summary = "; ".join(f"{d.node}: {d.result}" for d in devices)
    log(f"ALL DONE - {summary}")
    notify(f"Soil OTA babysitter finished.\n{summary}")
    set_active(False)
    client.loop_stop()
    return 0 if all(d.result == "updated+verified" for d in devices) else 1


if __name__ == "__main__":
    sys.exit(main())
