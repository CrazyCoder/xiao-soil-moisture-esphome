"""Safety checks: no stale retained ON after terminal jobs or connection failure."""

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import signal
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "ota_guard", ROOT / "homeassistant/addons/soil-builder-ota/bridge.py")
guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guard
SPEC.loader.exec_module(guard)

BUILD = {"job_id": "build-1", "configuration": "kedr.yaml", "job_type": "compile",
         "created_at": "2026-10-31T12:00:00Z", "status": "completed"}
DEVICE = {"configuration": "kedr.yaml", "queued_update": True}
WS_DEVICE = {"configuration": "kedr.yaml", "name": "kedr",
             "runtime_state": {"queued_update": True, "state": "offline"}}
OPTIONS = {"devices": [{"configuration": "kedr.yaml", "topic_prefix": "kedr"}],
           "max_wait_hours": 8, "max_awake_minutes": 10}


def upload(status):
    return {"job_id": "upload-1", "configuration": "kedr.yaml", "job_type": "upload",
            "created_at": "2026-10-31T13:00:00Z", "status": status, "port": "OTA"}


class PolicyTests(unittest.TestCase):
    def decide(self, jobs=None, lease=None, now=100, live=0, device=None):
        return guard.decide(DEVICE if device is None else device,
                            [BUILD] if jobs is None else jobs,
                            guard.Lease() if lease is None else lease,
                            now, live, 8 * 3600, 600)

    def test_sleeping_device_can_wait_hours_without_awake_timeout(self):
        lease, hold, _, _ = self.decide()
        lease, hold, _, _ = self.decide(lease=lease, now=6 * 3600)
        self.assertTrue(hold)
        self.assertEqual(lease.awake_since, 0)

    def test_awake_deadline_is_not_extended_by_further_reports(self):
        lease, _, _, _ = self.decide()
        lease, hold, _, _ = self.decide(lease=lease, now=150, live=150)
        self.assertTrue(hold)
        lease, hold, clear, _ = self.decide(lease=lease, now=751, live=750)
        self.assertFalse(hold)
        self.assertTrue(clear)
        self.assertTrue(lease.closed)

    def test_live_report_before_arming_is_not_a_wake(self):
        lease, _, _, _ = self.decide(now=100, live=99)
        self.assertEqual(lease.awake_since, 0)

    def test_terminal_uploads_always_release_and_close(self):
        for status in guard.TERMINAL:
            with self.subTest(status=status):
                lease, hold, clear, _ = self.decide(jobs=[BUILD, upload(status)])
                self.assertFalse(hold)
                self.assertTrue(clear)
                self.assertTrue(lease.closed)
                # Even if job history disappears / Builder keeps its flag,
                # this generation must not silently re-arm.
                _, hold, clear, _ = self.decide(lease=lease)
                self.assertFalse(hold)
                self.assertTrue(clear)

    def test_upload_completed_between_polls_is_detected(self):
        lease, _, _, _ = self.decide()
        _, hold, clear, _ = self.decide(lease=lease, jobs=[BUILD, upload("completed")])
        self.assertFalse(hold)
        self.assertTrue(clear)

    def test_new_explicit_install_can_retry(self):
        closed = guard.Lease("build-old", 50, closed=True)
        lease, hold, clear, _ = self.decide(lease=closed)
        self.assertEqual(lease.generation, BUILD["job_id"])
        self.assertTrue(hold)
        self.assertFalse(clear)

    def test_no_hold_during_compilation(self):
        for status in ("queued", "running", "failed", "cancelled"):
            _, hold, _, _ = self.decide(jobs=[dict(BUILD, status=status)])
            self.assertFalse(hold)

    def test_cancel_queue_or_remove_device_releases(self):
        for device in ({}, dict(DEVICE, queued_update=False)):
            _, hold, _, _ = self.decide(device=device)
            self.assertFalse(hold)

    def test_old_upload_does_not_close_new_build(self):
        old = dict(upload("completed"), created_at="2026-10-30T13:00:00Z")
        _, hold, clear, _ = self.decide(jobs=[BUILD, old])
        self.assertTrue(hold)
        self.assertFalse(clear)

    def test_never_woke_deadline_closes_request(self):
        lease = guard.Lease("build-1", 100)
        _, hold, clear, _ = self.decide(lease=lease, now=100 + 8 * 3600)
        self.assertFalse(hold)
        self.assertTrue(clear)


class FakeBroker:
    def __init__(self):
        self.last_live = {}
        self.commands = {}
        self.generation = 0
        self.calls = []
        self.fail_off = False

    async def publish(self, prefix, on):
        self.calls.append((prefix, on))
        if not on and self.fail_off:
            raise ConnectionError("broker unavailable")


class FakeBuilder:
    def __init__(self, jobs=None, device=None, fail_clear=False):
        self.jobs = [BUILD] if jobs is None else jobs
        self.device = WS_DEVICE if device is None else device
        self.fail_clear = fail_clear
        self.calls = []

    async def call(self, command, **args):
        self.calls.append(command)
        if command == "devices/list":
            return {"configured": [self.device]}
        if command == "firmware/get_jobs":
            return self.jobs
        if command == "firmware/clear_queued_update" and self.fail_clear:
            raise ConnectionError("Builder stopped")


class GuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "leases.json"
        self.broker = FakeBroker()
        self.service = guard.Guard(OPTIONS, self.path, self.broker)

    async def test_startup_off_is_acknowledged_before_on(self):
        await self.service.tick(FakeBuilder())
        self.assertEqual(self.broker.calls, [("kedr", False), ("kedr", True)])
        await self.service.tick(FakeBuilder())
        self.assertEqual(len(self.broker.calls), 2)  # do not refresh device watchdog

    async def test_nested_websocket_runtime_state_releases_when_queue_clears(self):
        await self.service.tick(FakeBuilder(device=WS_DEVICE))
        self.assertEqual(self.broker.calls[-1], ("kedr", True))
        cleared = dict(WS_DEVICE, runtime_state={"queued_update": False, "state": "online"})
        await self.service.tick(FakeBuilder(device=cleared))
        self.assertEqual(self.broker.calls[-1], ("kedr", False))

    async def test_flat_device_response_remains_supported(self):
        await self.service.tick(FakeBuilder(device=DEVICE))
        self.assertEqual(self.broker.calls[-1], ("kedr", True))

    async def test_confirmed_off_is_not_republished_on_repeated_cleanup(self):
        await self.service.cleanup()
        await self.service.cleanup()
        await self.service.cleanup()
        self.assertEqual(self.broker.calls, [("kedr", False)])

    async def test_cannot_arm_if_initial_off_is_unconfirmed(self):
        self.broker.fail_off = True
        with self.assertRaises(ConnectionError):
            await self.service.tick(FakeBuilder())
        self.assertNotIn(("kedr", True), self.broker.calls)

    async def test_off_failure_remains_pending_and_is_retried(self):
        await self.service.tick(FakeBuilder())
        self.broker.fail_off = True
        self.assertFalse(await self.service.cleanup())
        self.assertIsNone(self.service.sent["kedr.yaml"])
        self.broker.fail_off = False
        self.assertTrue(await self.service.cleanup())
        self.assertIs(self.service.sent["kedr.yaml"], False)

    async def test_off_does_not_depend_on_builder_accepting_clear(self):
        await self.service.tick(FakeBuilder())
        with self.assertRaises(ConnectionError):
            await self.service.tick(FakeBuilder([BUILD, upload("failed")], fail_clear=True))
        self.assertEqual(self.broker.calls[-1], ("kedr", False))
        self.assertTrue(self.service.leases["kedr.yaml"].closed)

    async def test_restart_preserves_closed_generation(self):
        await self.service.tick(FakeBuilder([BUILD, upload("cancelled")]))
        restarted = guard.Guard(OPTIONS, self.path, self.broker)
        self.broker.calls.clear()
        await restarted.cleanup()
        await restarted.tick(FakeBuilder())
        self.assertEqual(self.broker.calls, [("kedr", False)])

    async def test_restart_does_not_reset_awake_deadline(self):
        self.service.leases["kedr.yaml"] = guard.Lease("build-1", 100, 150)
        self.service.save()
        restarted = guard.Guard(OPTIONS, self.path, self.broker)
        with patch.object(guard.time, "time", return_value=751):
            await restarted.tick(FakeBuilder())
        self.assertNotIn(("kedr", True), self.broker.calls)

    async def test_completed_queue_releases_hold(self):
        await self.service.tick(FakeBuilder())
        await self.service.tick(FakeBuilder(device=dict(DEVICE, queued_update=False)))
        self.assertEqual(self.broker.calls[-1], ("kedr", False))
        self.assertTrue(self.service.leases["kedr.yaml"].closed)

    async def test_unexpected_retained_on_is_cleared_without_queue(self):
        await self.service.cleanup()
        self.broker.commands["kedr.yaml"] = b"ON"
        await self.service.tick(FakeBuilder(device=dict(DEVICE, queued_update=False)))
        self.assertEqual(self.broker.calls, [("kedr", False), ("kedr", False)])

    async def test_mqtt_reconnect_requires_off_before_rearming(self):
        await self.service.tick(FakeBuilder())
        self.broker.generation += 1
        self.broker.calls.clear()
        await self.service.tick(FakeBuilder())
        self.assertEqual(self.broker.calls, [("kedr", False), ("kedr", True)])

    async def test_incompatible_builder_fails_closed(self):
        with self.assertRaises(RuntimeError):
            await self.service.tick(FakeBuilder(device={"configuration": "kedr.yaml"}))
        self.assertNotIn(("kedr", True), self.broker.calls)

    async def test_corrupt_state_still_allows_off_but_never_on(self):
        self.path.write_text("{broken")
        restarted = guard.Guard(OPTIONS, self.path, self.broker)
        await restarted.cleanup()
        with self.assertRaises(RuntimeError):
            await restarted.tick(FakeBuilder())
        self.assertEqual(self.broker.calls, [("kedr", False)])

    async def test_rpc_ignores_events_and_matches_response_id(self):
        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            request_data = await ws.receive_json()
            await ws.send_json({"event": "device_state_changed", "data": {}})
            await ws.send_json({"message_id": request_data["message_id"],
                                "result": {"configured": [WS_DEVICE]}})
            await ws.close()
            return ws
        app = web.Application()
        app.router.add_get("/ws", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        server = await asyncio.get_running_loop().create_server(
            runner.server, "127.0.0.1", 0)
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        async with guard.aiohttp.ClientSession() as session:
            async with session.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
                result = await guard.Builder(ws).call("devices/list")
                self.assertEqual(result["configured"], [WS_DEVICE])

    async def test_rpc_error_names_command_and_code_without_server_details(self):
        class Socket:
            async def send_json(self, request):
                self.message_id = request["message_id"]

            def __aiter__(self):
                return self

            async def __anext__(self):
                response = {"message_id": self.message_id, "error_code": "unknown_command",
                            "details": "password=do-not-log-this"}
                return guard.aiohttp.WSMessage(guard.aiohttp.WSMsgType.TEXT,
                                               json.dumps(response), "")

        with self.assertRaises(guard.BuilderAPIError) as caught:
            await guard.Builder(Socket()).call("devices/list")
        self.assertEqual(str(caught.exception), "Builder command devices/list: unknown_command")
        self.assertNotIn("do-not-log-this", str(caught.exception))

    def test_unknown_error_code_cannot_inject_secrets_into_log(self):
        error = guard.BuilderAPIError("devices/list", "password=do-not-log-this")
        self.assertEqual(str(error), "Builder command devices/list: unrecognized_error")

    async def test_sigterm_interrupts_hung_builder_and_releases_hold(self):
        hanging = asyncio.Event()
        signals = {}
        closed = []
        broker = self.broker
        broker.start = lambda: None
        broker.close = lambda: closed.append(True)

        class Context:
            def __init__(self, value):
                self.value = value

            async def __aenter__(self):
                return self.value

            async def __aexit__(self, *args):
                return False

        class Socket:
            async def receive_json(self, **kwargs):
                return {"requires_auth": False}

        class Session:
            def ws_connect(self, *args, **kwargs):
                return Context(Socket())

        class HangingBuilder(FakeBuilder):
            async def call(self, command, **args):
                if command == "devices/list" and command in self.calls:
                    hanging.set()
                    await asyncio.Future()
                return await super().call(command, **args)

        options = dict(OPTIONS, builder_url="http://builder:6052")
        loop = asyncio.get_running_loop()
        with patch.object(guard, "Broker", return_value=broker), \
                patch.object(guard, "Builder", return_value=HangingBuilder()), \
                patch.object(guard.aiohttp, "ClientSession", return_value=Context(Session())), \
                patch.object(loop, "add_signal_handler",
                             side_effect=lambda sig, callback: signals.update({sig: callback})):
            task = asyncio.create_task(guard.serve(options, self.path))
            try:
                await asyncio.wait_for(hanging.wait(), timeout=4)
                self.assertEqual(broker.calls[-1], ("kedr", True))
                signals[signal.SIGTERM]()
                await asyncio.wait_for(task, timeout=1)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(broker.calls[-1], ("kedr", False))
        self.assertTrue(closed)


class MqttWireTests(unittest.IsolatedAsyncioTestCase):
    """Use the real Paho client against a tiny MQTT 3.1.1 test broker."""

    async def asyncSetUp(self):
        self.retained = {}
        self.frames = []
        self.drop_ack = False
        self.connections = []

        async def accept(reader, writer):
            self.connections.append(writer)
            try:
                while True:
                    header = (await reader.readexactly(1))[0]
                    length, multiplier = 0, 1
                    while True:
                        byte = (await reader.readexactly(1))[0]
                        length += (byte & 127) * multiplier
                        multiplier *= 128
                        if not byte & 128:
                            break
                    body = await reader.readexactly(length)
                    kind = header >> 4
                    if kind == 1:  # CONNECT
                        writer.write(b"\x20\x02\x00\x00")
                    elif kind == 8:  # SUBSCRIBE, one filter
                        writer.write(b"\x90\x03" + body[:2] + b"\x01")
                    elif kind == 3:  # PUBLISH
                        topic_length = int.from_bytes(body[:2])
                        topic = body[2:2 + topic_length].decode()
                        position = 2 + topic_length
                        qos = (header >> 1) & 3
                        packet_id = body[position:position + 2] if qos else b""
                        payload = body[position + (2 if qos else 0):]
                        self.frames.append((topic, payload, qos, bool(header & 1)))
                        if header & 1:
                            self.retained[topic] = payload
                        if qos == 1 and not self.drop_ack:
                            writer.write(b"\x40\x02" + packet_id)
                        # A subscriber receives a live echo, even when the
                        # originating publish used the retain flag.
                        await self.send(writer, topic, payload)
                    elif kind == 12:  # PINGREQ
                        writer.write(b"\xd0\x00")
                    elif kind == 14:  # DISCONNECT
                        break
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()

        self.server = await asyncio.start_server(accept, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        options = dict(OPTIONS, mqtt_host="127.0.0.1", mqtt_port=port,
                       mqtt_username="test", mqtt_password="test")
        self.broker = guard.Broker(options, asyncio.get_running_loop())
        self.broker.start()
        await self.until(lambda: self.broker.client.is_connected())

    async def asyncTearDown(self):
        await asyncio.to_thread(self.broker.close)
        for writer in self.connections:
            writer.close()
            await writer.wait_closed()
        self.server.close()
        await self.server.wait_closed()

    async def until(self, condition):
        async with asyncio.timeout(3):
            while not condition():
                await asyncio.sleep(0.01)

    async def send(self, writer, topic, payload, retain=False):
        encoded = topic.encode()
        body = len(encoded).to_bytes(2) + encoded + payload
        self.assertLess(len(body), 128)
        writer.write(bytes([0x31 if retain else 0x30, len(body)]) + body)
        await writer.drain()

    async def test_on_then_off_are_retained_qos1_and_not_wake_evidence(self):
        await self.broker.publish("kedr", True)
        await self.broker.publish("kedr", False)
        await self.until(lambda: self.broker.commands.get("kedr.yaml") == b"OFF")
        self.assertEqual(self.retained["kedr/ota"], b"OFF")
        self.assertEqual(self.frames, [("kedr/ota", b"ON", 1, True),
                                       ("kedr/ota", b"OFF", 1, True)])
        self.assertEqual(self.broker.last_live, {})

    async def test_missing_puback_is_failure_and_off_can_be_retried(self):
        self.drop_ack = True
        with patch.object(guard, "PUBLISH_ACK_TIMEOUT", 0.1):
            with self.assertRaises(ConnectionError):
                await self.broker.publish("kedr", False)
        self.drop_ack = False
        await self.broker.publish("kedr", False)
        self.assertEqual(self.retained["kedr/ota"], b"OFF")

    async def test_retained_sensor_replay_is_not_a_live_wake(self):
        writer = self.connections[0]
        await self.send(writer, "kedr/sensor/battery/state", b"90", retain=True)
        await asyncio.sleep(0.05)
        self.assertEqual(self.broker.last_live, {})
        await self.send(writer, "kedr/sensor/battery/state", b"90")
        await self.until(lambda: "kedr.yaml" in self.broker.last_live)


if __name__ == "__main__":
    unittest.main()
