# Soil Builder OTA Guard

This local Home Assistant app connects ESPHome Device Builder's offline update
queue to the firmware's retained `<name>/ota` hold. Use Builder's usual
**Install → Wirelessly** button; no separate uploader, API integration or manual
MQTT actions are needed once the guard is running.

## Requirements

- ESPHome Device Builder with queued offline installs and the WebSocket API
  documented for Builder 1.26.0. The Builder version is distinct from the
  ESPHome firmware tool version.
- Guard **1.0.1 or newer**. Version 1.0.0 did not read Builder's nested
  `runtime_state` correctly and could disconnect immediately after connecting.
- A working MQTT broker and device YAML files already present in Builder.
- Firmware **1.6.0 or newer** for independent post-update OFF cleanup. Install
  it once through Builder with the guard enabled. Keep each device's local
  `ota.encryption` key; encrypted OTA requires ESPHome **2026.9.0 or newer**.
  The guard never reads the key or changes how Builder uploads the firmware.

The shared firmware package already contains `esphome:`, `logger:` and `ota:`.
Do not add a generated `api:` block to use Builder. These devices communicate
with Home Assistant through MQTT and do not need adding to the ESPHome
integration. An old ESPHome discovery prompt can be ignored. Set device names
through `substitutions`; retain a local `ota:` override only for settings such
as encryption.

## Install

1. Use a current ESPHome Device Builder with queued offline installs and its
   WebSocket API (the documented API in Builder 1.26.0 is supported). This is
   the **Builder version**, not the ESPHome firmware version.
2. Open the **`local_apps` Samba share** on Home Assistant and copy this entire
   directory into it as `soil-builder-ota`. The resulting path within the share
   must be `soil-builder-ota/config.yaml`. The share is separate from `config`:
   do not create `config/local_apps` or `config/addons`, and do not put the app
   in `config/esphome`. Older Samba versions called this share `addons`; use
   `local_apps` on current installations. The source contains `config.yaml`,
   `Dockerfile`, `requirements.txt` and `bridge.py`; keep all four together.
3. In Home Assistant, open the app/add-on store, refresh/check for updates,
   then install **Soil Builder OTA Guard** under local apps.
4. Configure the broker login and the Builder URL. For the HA Builder app, get
   its **Ingress port** from `ha addons info 5c53de3b_esphome` in the HA terminal
   (use your actual app slug if different). Set `builder_url` to
   `http://127.0.0.1:<ingress_port>` and leave `builder_username` and
   `builder_password` empty. The guard uses host networking, so it can reach
   the trusted loopback listener without exposing Builder to the LAN. Do not
   enable "Disable external authentication" or open port 6052 for this.
   The default port 6052 is for a standalone dashboard and may be closed in
   the HA app. A standalone Builder can use its own username/password; those
   are not Home Assistant credentials. Mosquitto defaults to local port 1883.
5. The defaults manage both `kedr.yaml` → `kedr` and `palm.yaml` → `palm`. Add one
   mapping for each additional device to manage. Use the actual Builder filename
   and MQTT prefix. The guard deliberately does not control unlisted devices.
6. Start the guard and enable **Start on boot** and the Supervisor **Watchdog**.
   The log should report an acknowledged retained OFF, followed by a Builder
   connection. Never put actual passwords in this repository.

The installed directory should look like this (not an extra nested copy of
`soil-builder-ota` inside itself):

```text
local_apps/
└── soil-builder-ota/
    ├── config.yaml
    ├── Dockerfile
    ├── requirements.txt
    ├── bridge.py
    ├── icon.png
    ├── icon.svg
    ├── logo.png
    ├── README.md
    └── CHANGELOG.md
```

No entry in Home Assistant's `configuration.yaml` is needed. If the app does
not appear after checking for updates and refreshing the store page, inspect
the **Supervisor** logs for configuration validation errors.

### Guard options for Kedr and Palm

Configure these options in the **Guard app's Configuration tab**, not in
`configuration.yaml` or the device YAML. For example:

```yaml
# 62123 is an EXAMPLE: replace it with Builder's actual ingress_port.
builder_url: http://127.0.0.1:62123
builder_username: ""
builder_password: ""
mqtt_host: 127.0.0.1
mqtt_port: 1883
mqtt_username: "YOUR_MQTT_USER"
mqtt_password: "YOUR_MQTT_PASSWORD"
devices:
  - configuration: kedr.yaml
    topic_prefix: kedr
  - configuration: palm.yaml
    topic_prefix: palm
max_wait_hours: 8
max_awake_minutes: 10
```

`builder_username` and `builder_password` apply to a standalone Builder with
its own authentication. The HA Builder app uses Home Assistant Ingress instead
and has no separate dashboard account to configure. Leave these fields empty
for the local Ingress connection. The MQTT login is separate and is required
if your broker requires authentication.

After starting the guard, expect a retained OFF acknowledgement for each
device, followed by a message like:

```text
Connected to Builder (version ..., ESPHome ...); watching queued installs
```

With no queued updates, quiet logs are normal. Guard queries Builder every
two seconds; it does not poll or wake the sensors. Repeated warnings are a
connection/API problem, not normal device checking.

### Updating an existing local installation

Replace the files in `local_apps/soil-builder-ota`, check for updates in the
app store, and select **Update** for Soil Builder OTA Guard. A restart alone
does not rebuild its Docker image with the changed Python code. Keep your
existing app options, including the working Ingress URL and broker login.

Version **1.0.2** adds the app icon (`icon.png`, 128 × 128) and matching detail
page logo (`logo.png`, 256 × 256). Keep these files next to `config.yaml`.
After copying the assets, check for store updates and refresh the browser page
to reload the images. The editable artwork is in `icon.svg`.

Version **1.0.1** fixes the nested `runtime_state.queued_update` returned by
Builder's WebSocket API, while retaining support for flat device responses.
It logs the Builder/ESPHome versions and safe command/error-code diagnostics
without printing server details or credentials. Retries after connection/API
failures back off from 5 to 60 seconds; a confirmed OFF is not republished on
each retry. Failed OFF deliveries still retry until the broker acknowledges.

## Updating a sleeping device

1. Edit/save the device YAML in Builder.
2. While the device is Offline, select **Install → Wirelessly** using the
   normal automatic OTA target. Builder compiles and arms the offline queue.
3. The guard sees the ready queue and publishes retained `kedr/ota = ON` with
   QoS 1. It waits for the broker's PUBACK before considering that successful.
4. On its next natural wake, Kedr receives ON, reports over MQTT and stays
   awake. Builder detects it and uploads its prepared firmware.
5. The guard publishes retained **OFF** when the upload finishes, fails or is
   cancelled. On success, the firmware also publishes OFF after its first
   complete MQTT report and OTA validation. Kedr then sleeps normally, unless
   a real USB host or an active calibration/settings operation still needs it.

Do not run the old OTA babysitter or publish manual ON holds for these same
devices while the guard is running: the guard clears ON when there is no
ready queued install. Stop the guard first if you need a manual maintenance
session. A plain Compile does not arm a wake hold.

## Verify the complete cycle

Start with one device, such as Kedr, **on battery without a USB connection to
a computer**. A detected USB host intentionally holds the device awake, even
after the OTA hold has been released.

### Successful update

1. While Kedr is **Offline**, select **Install → Wirelessly** in Builder. It
   is fine to reinstall the current firmware without changing its behavior.
2. After compilation, Builder should show a queued update. Guard should log:
   `kedr.yaml: retained ON acknowledged (waiting for Builder upload)`.
3. Wait for the next natural wake. For a quick first check, press the device
   button once instead. Builder should begin uploading automatically.
4. Wait for a successful upload and check the device's **Firmware build** and
   fresh sensor reports. Guard should log:
   `kedr.yaml: retained OFF acknowledged`.
5. Verify the broker's saved OFF using the fresh subscription described below.
6. After the report, the device should return to **Offline in Builder** within
   seconds if no USB/calibration/settings hold remains. HA sensor values stay
   available during normal sleep; Kedr's configured 12-hour expiry therefore
   cannot be used to check whether the device has just gone to sleep.

### Cancel before the device wakes

Queue another offline install and wait for the retained ON acknowledgement.
Cancel/clear the **waiting update in Builder** before the device wakes. Guard
must publish OFF; verify the retained value by subscribing again. The device
should not acquire an OTA hold at its next wake. A new Install creates a new
request if you later want to retry.

Both the successful-update and cancellation scenarios were verified in Home
Assistant during deployment on 2026-10-31. The error/disconnection/deadline
paths are also covered by the automated tests described below.

### Check the retained value in Home Assistant

1. Go to **Settings → Devices & services → MQTT → Configure**. Use the MQTT
   integration, not the Mosquitto app's configuration page.
2. Under **Listen to a topic**, enter `kedr/ota` and select **Start listening**.
3. After update completion or cancellation, the initial message should be:

   ```text
   OFF
   Retain: true
   ```

If listening was already active during the update, stop it and start again.
A fresh subscription receiving OFF with `Retain: true` checks the broker's
stored value, rather than only the live OFF sent during the update. Use
`palm/ota` to repeat the check for Palm. Do not publish ON just to test the
guard; ON without a queued install is deliberately cleared.

## Troubleshooting

| Symptom | Check or fix |
|---|---|
| App absent from the store | Copy into the separate `local_apps` share, check the directory structure, then check for store updates and refresh. Inspect Supervisor logs for manifest errors. No `configuration.yaml` entry is needed. |
| `ClientConnectorError` and repeated OFF logs | MQTT can be working while Builder is unreachable. Check the actual `ingress_port` and `builder_url`; 6052 is often closed in the HA Builder app. Recheck the port if this starts after a Builder reinstall/reconfiguration. |
| Connection succeeds, immediately followed by `RuntimeError` | Update Guard to 1.0.1 or newer using **Update**, not just Restart. It supports WebSocket's nested `runtime_state.queued_update`; newer logs identify safe command/error codes. |
| `not_authenticated` | Check whether the URL points to the local HA Ingress listener or a standalone Builder requiring its own login. HA credentials do not configure a standalone Builder account. |
| No ON after compiling | Use an offline **Install → Wirelessly**, not Compile alone; verify both the Builder filename and MQTT topic prefix in Guard options. |
| OFF acknowledged but the device stays online | Check for a USB host or an active calibration/settings operation. A broker acknowledgement confirms OFF delivery, not the absence of other holds. |
| `OFF unconfirmed; will retry` | Broker cleanup is not confirmed. Restore MQTT connectivity; Guard retries, and the firmware has an independent local timeout. |

## Battery protection

- OFF is published on success, error, cancellation, queue removal, Builder
  connection failure, app shutdown and app startup. Cleanup never depends on
  Builder answering another request.
- MQTT acknowledgements are required. An unsuccessful OFF is retried after
  reconnect; logs explicitly say when cleanup is unconfirmed. An unexpected
  retained ON without an active queued request is cleared too.
- `max_wait_hours` defaults to 8 hours, allowing the normal 1–4 hour schedule
  and the 6 hour network-failure backoff. If the device never wakes, OFF is
  published and its Builder queue is cleared.
- `max_awake_minutes` defaults to 10 minutes. Only a **live**, non-retained
  sensor message starts this deadline; a cached Online badge or replayed
  retained reading does not count. Further readings do not extend it.
- Deadlines and closed build IDs persist in `/data/leases.json`. Restarting
  does not silently retry a failed/cancelled/expired install. Press Install
  again to create a new request. Damaged state disables ON until repaired;
  OFF cleanup remains enabled.
- Firmware independently clears the retained hold after an OTA boot or OTA
  error/abort, and retains a retry marker until it receives the broker's OFF
  echo. It explicitly stores that marker before flashing or sleeping. Its
  existing 10 minute OTA watchdog also uses this cleanup path.

No networked controller can guarantee delivery while the broker or network is
unreachable. The firmware's local watchdog is the independent battery backstop;
the persistent retry marker clears a stale broker hold when connectivity returns.
The firmware's absolute awake cap remains in force for battery operation.

An upload acknowledgement means the image was delivered, not that the new
application is healthy. Check the device's Firmware build and sensor reports
after installation; the post-boot cleanup runs after the full report.

## Testing outside Home Assistant

```sh
python -m venv /tmp/soil-guard-venv
/tmp/soil-guard-venv/bin/pip install -r homeassistant/addons/soil-builder-ota/requirements.txt
/tmp/soil-guard-venv/bin/python -m unittest discover -s tests -v
```

The tests cover queue/error/cancellation/restart policy, a real WebSocket
exchange, and the real Paho client talking to a small test MQTT broker. They
check retained QoS 1 ON/OFF, missing PUBACK recovery and retained-reading replay.
Firmware code generation/builds use `python tools/check-builder.py --compile`.

The Builder API contract is documented in
[esphome/device-builder](https://github.com/esphome/device-builder/blob/1.26.0/docs/API.md).
