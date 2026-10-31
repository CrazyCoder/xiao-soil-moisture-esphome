# Changelog

## 1.0.2

- Add a plant/shield icon and matching app-page logo for Home Assistant.
- Include the editable SVG source with the app assets.

## 1.0.1

- Read queued-update state from WebSocket devices' nested `runtime_state`.
- Preserve compatibility with flat device responses.
- Include safe command/error codes and Builder versions in diagnostics.
- Back off failed API connections from 5 to 60 seconds.
- Avoid republishing an already acknowledged OFF on every retry; unconfirmed
  cleanup and unexpected retained ON still trigger OFF.
- Document the trusted local Ingress port for Home Assistant Builder installs.

## 1.0.0

- Bridge Builder's offline queue to retained MQTT wake holds.
- Release holds after upload completion, errors, cancellation and disconnects.
- Persist deadlines and closed requests, with independent firmware cleanup.
