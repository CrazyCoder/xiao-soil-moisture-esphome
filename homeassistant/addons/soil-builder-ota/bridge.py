"""Bridge Builder's completed offline builds to short-lived MQTT wake holds.

This process never compiles or uploads firmware and never reads OTA keys.
Builder owns the queue and its encrypted esphome upload subprocesses.
"""

import asyncio
from dataclasses import asdict, dataclass
import json
import logging
from pathlib import Path
import signal
import sys
import time
from urllib.parse import urlsplit, urlunsplit

import aiohttp
import paho.mqtt.client as mqtt

LOG = logging.getLogger("soil-builder-ota")
TERMINAL = {"completed", "failed", "cancelled"}
PUBLISH_ACK_TIMEOUT = 5


class GuardError(RuntimeError):
    """An internally constructed diagnostic, safe to print without secrets."""


class BuilderAPIError(GuardError):
    def __init__(self, command, code):
        allowed = {"invalid_message", "unknown_command", "invalid_args", "not_found",
                   "already_exists", "internal_error", "not_authenticated", "rate_limited",
                   "unavailable", "precondition_failed", "no_pairing_window",
                   "no_compatible_peer"}
        safe_code = code if isinstance(code, str) and code in allowed else "unrecognized_error"
        super().__init__(f"Builder command {command}: {safe_code}")


def normalize_device(device):
    """WebSocket devices nest runtime state; the legacy REST API flattens it."""
    runtime = device.get("runtime_state")
    if runtime is not None:
        if not isinstance(runtime, dict):
            raise GuardError("devices/list: runtime_state is not an object")
        return {**device, **runtime}
    return device


@dataclass
class Lease:
    generation: str = ""
    pending_since: float = 0
    awake_since: float = 0
    closed: bool = False


def decide(device, jobs, lease, now, last_live, max_wait, max_awake):
    """Return (new lease, should hold, should clear queue, reason). Pure policy."""
    builds = [j for j in jobs if j["job_type"] == "compile"]
    build = max(builds, key=lambda j: (j["created_at"], j["job_id"]), default=None)
    queued = device.get("queued_update", False)
    if not queued:
        # Builder normally clears the flag before the next poll can see the
        # terminal upload. Persist closure anyway: a stale restored flag must
        # not re-enable ON for the request we just finished/cancelled.
        if lease.generation:
            lease = Lease(**dict(asdict(lease), closed=True))
        return lease, False, False, "queue cleared"
    if build is None or build["status"] != "completed":
        return lease, False, False, "no completed queued build"
    if lease.generation != build["job_id"]:
        lease = Lease(build["job_id"], now)
    else:
        lease = Lease(**asdict(lease))
    uploads = [j for j in jobs if j["job_type"] == "upload"
               and j.get("port") == "OTA" and not j.get("flash_bootloader", False)
               and j["created_at"] >= build["created_at"]]
    upload = max(uploads, key=lambda j: (j["created_at"], j["job_id"]), default=None)
    if upload and upload["status"] in TERMINAL:
        lease.closed = True
        return lease, False, True, "upload " + upload["status"]
    if lease.closed:
        return lease, False, True, "request already closed; install again to retry"
    # Only a LIVE device publication starts the awake timer. Retained states
    # and Builder's possibly stale Online badge cannot expire a sleeping lease.
    if last_live >= lease.pending_since and not lease.awake_since:
        lease.awake_since = last_live
    if now - lease.pending_since >= max_wait:
        lease.closed = True
        return lease, False, True, "device did not wake within deadline"
    if lease.awake_since and now - lease.awake_since >= max_awake:
        lease.closed = True
        return lease, False, True, "awake deadline reached"
    return lease, True, False, "waiting for Builder upload"


class Builder:
    def __init__(self, ws):
        self.ws = ws
        self.sequence = 0

    async def call(self, command, **args):
        self.sequence += 1
        message_id = str(self.sequence)
        await self.ws.send_json({"command": command, "message_id": message_id, "args": args})
        async with asyncio.timeout(15):
            async for message in self.ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    raise ConnectionError("Builder WebSocket closed")
                response = json.loads(message.data)
                if response.get("message_id") != message_id:
                    continue
                if "error_code" in response:
                    # Do not log server details: authentication errors can
                    # contain sensitive request context in some versions.
                    raise BuilderAPIError(command, response["error_code"])
                if "result" in response:
                    return response["result"]
        raise ConnectionError("Builder WebSocket ended")


class Broker:
    def __init__(self, options, loop):
        self.options = options
        self.loop = loop
        self.last_live = {}
        self.commands = {}
        self.generation = 0
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id="soil-builder-ota-guard")
        self.client.username_pw_set(options["mqtt_username"], options["mqtt_password"])
        self.client.on_connect = self.on_connect

        def on_message(client, userdata, message):
            for entry in options["devices"]:
                if message.topic == entry["topic_prefix"] + "/ota":
                    loop.call_soon_threadsafe(self.commands.__setitem__,
                                             entry["configuration"], message.payload)
            # Our own /ota command is NOT evidence that the device woke.
            if message.retain:
                return
            for entry in options["devices"]:
                prefix = entry["topic_prefix"] + "/"
                if message.topic.startswith(prefix) and message.topic.endswith("/state"):
                    loop.call_soon_threadsafe(self.last_live.__setitem__,
                                             entry["configuration"], time.time())
        self.client.on_message = on_message

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            LOG.error("MQTT connection rejected")
            return
        self.loop.call_soon_threadsafe(self.reconnected)
        for entry in self.options["devices"]:
            client.subscribe(entry["topic_prefix"] + "/#", qos=1)

    def reconnected(self):
        self.generation += 1

    def start(self):
        self.client.connect_async(self.options["mqtt_host"], self.options["mqtt_port"])
        self.client.loop_start()

    async def publish(self, prefix, on):
        def publish_and_wait():
            if not self.client.is_connected():
                raise ConnectionError("MQTT disconnected")
            result = self.client.publish(prefix + "/ota", "ON" if on else "OFF",
                                         qos=1, retain=True)
            result.wait_for_publish(timeout=PUBLISH_ACK_TIMEOUT)
            if result.rc != mqtt.MQTT_ERR_SUCCESS or not result.is_published():
                raise ConnectionError("MQTT publish not acknowledged")
        await asyncio.to_thread(publish_and_wait)

    def close(self):
        self.client.disconnect()
        self.client.loop_stop()


class Guard:
    def __init__(self, options, state_path, broker):
        self.options, self.state_path, self.broker = options, state_path, broker
        self.leases = {}
        self.state_corrupt = False
        if state_path.exists():
            try:
                saved = json.loads(state_path.read_text())
                self.leases = {key: Lease(**value) for key, value in saved.items()}
            except (OSError, ValueError, TypeError, AttributeError):
                # Releasing OFF is still possible, but do not reset deadlines
                # or resurrect a cancelled request from damaged state.
                self.state_corrupt = True
                LOG.error("Lease state unreadable; only OFF cleanup is allowed")
        self.saved = None
        self.broker_generation = broker.generation
        # None means broker state is unknown: OFF must be acknowledged before
        # any ON, including after restart or either connection breaking.
        self.sent = {d["configuration"]: None for d in options["devices"]}

    def save(self):
        content = json.dumps({k: asdict(v) for k, v in self.leases.items()})
        if content == self.saved:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.with_suffix(".tmp")
        temp.write_text(content)
        temp.replace(self.state_path)
        self.saved = content

    async def off(self, entry):
        configuration = entry["configuration"]
        # Don't mark OFF locally until the broker confirms it. A failed OFF
        # therefore remains queued for cleanup on the next reconnect.
        await self.broker.publish(entry["topic_prefix"], False)
        self.sent[configuration] = False
        LOG.info("%s: retained OFF acknowledged", configuration)

    async def cleanup(self):
        success = True
        reconnected = self.broker_generation != self.broker.generation
        for entry in self.options["devices"]:
            config = entry["configuration"]
            if (not reconnected and self.sent[config] is False
                    and self.broker.commands.get(config) != b"ON"):
                continue  # this OFF already has a PUBACK; don't spam the broker
            try:
                await self.off(entry)
            except Exception:
                self.sent[entry["configuration"]] = None
                success = False
                LOG.error("%s: OFF unconfirmed; will retry", entry["configuration"])
        if success:
            self.broker_generation = self.broker.generation
        return success

    async def tick(self, builder):
        if self.state_corrupt:
            raise GuardError("repair lease state before arming updates")
        if self.broker_generation != self.broker.generation:
            if not await self.cleanup():
                raise ConnectionError("MQTT reconnect cleanup incomplete")
            self.broker_generation = self.broker.generation
        response = await builder.call("devices/list")
        if not isinstance(response, dict) or not isinstance(response.get("configured"), list):
            raise GuardError("devices/list: missing configured device list")
        devices = {d["configuration"]: normalize_device(d) for d in response["configured"]}
        for entry in self.options["devices"]:
            config = entry["configuration"]
            device = devices.get(config, {})
            jobs = await builder.call("firmware/get_jobs", configuration=config)
            # API guards: an older incompatible Builder must fail closed.
            if device and "queued_update" not in device:
                raise GuardError("devices/list: queued_update absent in device/runtime_state; check Builder version")
            lease, hold, clear, reason = decide(
                device, jobs, self.leases.get(config, Lease()), time.time(),
                self.broker.last_live.get(config, 0),
                self.options["max_wait_hours"] * 3600,
                self.options["max_awake_minutes"] * 60,
            )
            # Persist deadlines and closed generations BEFORE any MQTT change.
            self.leases[config] = lease
            self.save()
            if not hold:
                if self.sent[config] is not False or self.broker.commands.get(config) == b"ON":
                    await self.off(entry)
            elif self.sent[config] is not True:
                if self.sent[config] is None:
                    await self.off(entry)
                await self.broker.publish(entry["topic_prefix"], True)
                self.sent[config] = True
                LOG.info("%s: retained ON acknowledged (%s)", config, reason)
            # OFF comes FIRST; a Builder outage must never prevent cleanup.
            if clear:
                LOG.info("%s: closing queued request (%s)", config, reason)
                await builder.call("firmware/clear_queued_update", configuration=config)


def websocket_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("builder_url must be an http(s) URL")
    return urlunsplit(("wss" if parsed.scheme == "https" else "ws", parsed.netloc,
                       parsed.path.rstrip("/") + "/ws", "", ""))


async def serve(options, state_path):
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    worker = asyncio.current_task()

    def request_stop():
        if not stop.is_set():
            stop.set()
            # Interrupt an unresponsive Builder immediately so Supervisor's
            # stop grace period is spent on OFF, not a WebSocket timeout.
            worker.cancel()

    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, request_stop)
    broker = Broker(options, loop)
    broker.start()
    guard = Guard(options, state_path, broker)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            retry_delay = 5
            while not stop.is_set():
                try:
                    # Reconcile stale retained ON even before Builder is up.
                    if not await guard.cleanup():
                        raise ConnectionError("waiting for MQTT cleanup")
                    async with session.ws_connect(websocket_url(options["builder_url"]),
                                                  heartbeat=15, timeout=10) as ws:
                        info = await ws.receive_json(timeout=15)
                        builder = Builder(ws)
                        if info.get("requires_auth"):
                            await builder.call("auth/login",
                                               username=options["builder_username"],
                                               password=options["builder_password"])
                        LOG.info("Connected to Builder (version %s, ESPHome %s); watching queued installs",
                                 safe_version(info.get("server_version")),
                                 safe_version(info.get("esphome_version")))
                        while not stop.is_set():
                            await guard.tick(builder)
                            retry_delay = 5
                            try:
                                await asyncio.wait_for(stop.wait(), timeout=2)
                            except TimeoutError:
                                pass
                except Exception as error:
                    # Do not log exception text/URLs containing credentials.
                    diagnostic = str(error) if isinstance(error, GuardError) else type(error).__name__
                    LOG.warning("Connection/API failure (%s); releasing all holds; retry in %ss",
                                diagnostic, retry_delay)
                finally:
                    await guard.cleanup()
                if not stop.is_set():
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=retry_delay)
                    except TimeoutError:
                        pass
                    retry_delay = min(60, retry_delay * 2)
    except asyncio.CancelledError:
        if not stop.is_set():
            raise
    finally:
        # Retry SIGTERM cleanup briefly; firmware has an independent watchdog
        # for power loss, SIGKILL or an unreachable broker.
        for attempt in range(3):
            if await guard.cleanup():
                break
            await asyncio.sleep(1)
        broker.close()


def safe_version(value):
    # Never print arbitrary greeting data from an unauthenticated endpoint.
    if isinstance(value, str) and value and len(value) <= 48:
        if all(c in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.-+_" for c in value):
            return value
    return "unknown"


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    options = json.loads(Path(sys.argv[1]).read_text())
    names = [d["configuration"] for d in options["devices"]]
    prefixes = [d["topic_prefix"] for d in options["devices"]]
    if len(names) != len(set(names)) or len(prefixes) != len(set(prefixes)):
        raise ValueError("Each configuration and topic_prefix must be unique")
    if any(not p or any(c in p for c in "+#\0") or p.endswith("/") for p in prefixes):
        raise ValueError("Invalid MQTT topic prefix")
    asyncio.run(serve(options, Path(sys.argv[2])))


if __name__ == "__main__":
    main()
