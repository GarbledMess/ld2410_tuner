# LD2410 missing settings and unsuccessful Apply

## What the evidence establishes

Intermittently missing gate thresholds, limits, timeout and distance resolution
following a restart are consistent with an unsuccessful initial configuration
read. A working ESPHome API connection does not prove the radar settings loaded.
In ESPHome 2026.9.0, LD2410 setup calls `read_all_info()` once, with no readiness
retry. Baud Rate is populated from the configured UART rate, so its displayed
256000 is not evidence that the radar answered.

The warning `Invalid header: F4:F3:F2:F1` means the command-reply parser encountered
the normal streaming-data header. It supports a serial framing/receive problem;
it does not alone identify a full receive buffer, electrical noise or a startup
race. The driver issues several queries with blocking delays before returning to
its receive loop. A larger UART receive buffer and delayed startup queries are
mitigations for buffering/timing, not proof of the underlying physical cause.

Bluetooth changes schedule a radar restart followed by another configuration
read. This explains why toggling Bluetooth can restore the values. Use the
LD2410 Query Params button first, then Radar Restart if necessary. The existing
ESPHome `platform: restart` button restarts the ESP; the LD2410 restart button
restarts the radar. Neither action is a factory reset.

Source: [ESPHome 2026.9.0 LD2410 driver](https://github.com/esphome/esphome/blob/2026.9.0/esphome/components/ld2410/ld2410.cpp).

## Complete LD2410C package (recommended)

Use [ld2410c.yaml](../examples/esphome/ld2410c.yaml) to own the entire radar
configuration and recovery logic in one portable file. Keep each device's existing
`uart:` pin settings; no pin substitutions or ID additions are required for a
single-UART node. For the supplied example, the radar-specific part becomes:

```yaml
uart:
  tx_pin: GPIO4
  rx_pin: GPIO3

packages:
  ld2410: !include packages/ld2410c.yaml
```

Use **that device's existing pins**, not necessarily GPIO4/GPIO3. Existing baud
rate, parity, stop bits, buffer size and UART ID settings can also remain: local
values override package defaults. The defaults are 256000 baud, NONE parity, one
stop bit and a 1024-byte receive buffer. If the local configuration explicitly
sets a smaller buffer, remove that override to use the package's default.

Copy just `ld2410c.yaml` into a shared `packages/` directory alongside your ESPHome
node YAML files. All nodes in that directory can use the same file with one
include each. It has no sibling includes or additional project-file dependencies.

Once this file has been committed and pushed to this repository, the same
package can instead be loaded directly from GitHub:

```yaml
packages:
  ld2410: github://GarbledMess/ld2410_tuner/examples/esphome/ld2410c.yaml@main
```

This is ESPHome's [remote package syntax](https://esphome.io/components/packages/).
GitHub changes are used on a subsequent ESPHome build, subject to its package
cache; they do not automatically flash nodes, and a HACS update does not update
ESP firmware. A released tag or commit can replace `main` when you want to pin
firmware inputs. Until pushed, use the local include above.

### What to remove and keep

Remove the old `ld2410:` block and **only** the `platform: ld2410` entries under
`binary_sensor`, `sensor`, `number`, `select`, `switch`, `text_sensor`, and `button`.
Remove any separate inclusion of the recovery-only package. Keep other entries in
those sections; remove an empty section header if nothing remains underneath it.

Keep `esphome` name/friendly name, board/framework, Wi-Fi, API encryption, OTA,
logger, UART pins, and non-radar entities such as Status, WiFi Signal, ESP
Temperature, IP Address, SSID, ESP Restart and Safe Mode. Preserve unrelated
scripts and boot/interval automations. If the old recovery logic was pasted
inline, remove its matching helper/script/buttons too, so it is not installed
twice. The new package defines all recovery IDs internally.

The package retains the supplied example's radar names, presence delays (500 ms
on / 5 s off), one-second main sensor throttles and two-second gate throttles. It
includes all G0–G8 energies and thresholds, distance/energy sensors, presence
states, selects, switches and radar metadata. Query Params and Radar Restart are
included in the same file. It does not reset saved thresholds or change Bluetooth.
After failed parameter queries, it can restart the radar with a one-minute cooldown and
request restoration of Engineering Mode if it was reported on before that restart.

Keeping the device identity and entity names preserves the inputs used to derive
existing entity IDs. Check any different radar names or custom automations before
removing their old definitions. The presence name and timing/filter intervals can
be customized per node without editing the shared file:

```yaml
substitutions:
  ld2410_presence_name: Existing Presence Name
  ld2410_presence_off_delay: 10s
```

Optional settings are `ld2410_presence_name` (Presence), `ld2410_presence_on_delay`
(500ms), `ld2410_presence_off_delay` (5s), `ld2410_sensor_throttle` (1s), and
`ld2410_gate_throttle` (2s). No substitutions are needed when using the defaults.
Other custom radar names or logic need a tailored configuration; an existing
standalone ESPHome setup remains supported by the tuner without adopting this file.

The complete package targets **one radar on one UART per node**. If a node has
additional UARTs or radars, it needs explicit per-bus/per-radar mapping rather
than this automatic single-UART selection.

## Existing firmware without the package

Using this package is optional. Existing ESPHome LD2410 firmware can keep its
current configuration. To expose the tuner's optional recovery capability, add
this entry to the existing `button:` list (or add these fields to an existing
LD2410 button entry):

```yaml
button:
  - platform: ld2410
    query_params:
      name: Query Params
    restart:
      name: Radar Restart
```

For more than one radar, specify the matching `ld2410_id`. Keep a recognizable
`_query_params` or `_query_parameters` HA entity-ID suffix for automatic discovery.
The buttons do not require the package's internal IDs or scripts. Setting
`rx_buffer_size: 1024` on the existing radar UART is also independent of the package.

The complete package waits five seconds after boot and attempts up to three
parameter reads, two seconds apart, only while settings are missing. If they are
still missing, it attempts one **radar** restart, waits three seconds for the
driver's restart/reread sequence, requests Engineering Mode again if it was
reported on immediately before the restart, then retries parameter reads up to
three times. A one-minute cooldown starts before sending the restart command,
including when communication fails. The ESP itself is never automatically restarted.

A once-per-minute check runs recovery whenever settings are missing. It can
restart the radar again once the cooldown has elapsed, so recurring faults do not
require an ESP reboot to regain recovery. A persistent fault can trigger repeated
recovery cycles, but automatic radar restarts cannot occur more than once per
minute. Recovery stops when the readiness fields have values. Saved thresholds and Bluetooth are not rewritten. Restoring Engineering
Mode is a best-effort command, not an acknowledgement; an unknown pre-restart
mode cannot be reconstructed. A radar restart briefly interrupts presence reports.

This is fault recovery, not a confirmed root-cause fix. The radar restart uses
the same `restart_and_read_all_info()` method invoked after a Bluetooth change
in the ESPHome driver. It covers a failure that rereading alone cannot resolve,
but it does not reproduce the Bluetooth setting change itself and cannot repair
a persistently broken UART or power supply.

The readiness check detects missing initial values; it cannot detect a later
stalled link whose ESPHome number states remain cached. Validate the merged node
configuration before installing. Test several cold starts and ESP restarts: the
settings are expected to become numeric without touching Bluetooth when either
query retries or the restart recovery recovers the fault. That outcome has not
been demonstrated on the affected hardware. Re-enable Engineering Mode after a
manual radar restart if per-gate energies are missing. Check power, common ground,
UART connections and wiring if framing warnings persist.

References: [LD2410 buttons](https://esphome.io/components/sensor/ld2410/#button),
[UART receive buffer](https://esphome.io/components/uart/).

## What the tuner checks

The ESPHome package is optional. The tuner uses Home Assistant's exposed
radar entities, never the C++ IDs or recovery script from this package. A
healthy device without Query Params can Learn and Apply normally; missing
settings are rejected clearly when no query capability is available. Per-gate
engineering energy sensors and writable threshold entities still need to be
exposed with the gate naming convention described in the main README. A package
cannot make hidden entities available to the tuner without a firmware update.

Apply requires a complete current-model result and matching
configuration. It refuses writes while either half of a required move/still gate
pair, or an exposed distance limit, is missing. It can press an enabled,
unambiguous Query Params button on the same device to recover missing values,
with at most two attempts. It never automatically toggles Bluetooth or restarts
the radar. If recovered values differ from the configuration displayed when you
clicked Apply, refresh and review them before trying the selected saved result again.
The legacy Apply request without a saved-result selection still needs Learn again
when its original configuration has changed.

Writes are serialized per device, including manual threshold writes. Changed
values are spaced by one second; an available Query Params button requests a
reread after each change, followed by another one-second settling period. Already
matching values are not rewritten. A reported mismatch or failed call stops later
writes; errors identify partial completion and survive panel polling. Apply
rejections and incomplete outcomes are recorded in Home Assistant's logs.

This catches dropped calls and values that revert, but **matching HA states do
not prove radar persistence**. ESPHome's
[threshold number implementation](https://github.com/esphome/esphome/blob/2026.9.0/esphome/components/ld2410/number/gate_threshold_number.cpp)
publishes the requested value before attempting the radar command. An unanswered
query can therefore leave an optimistic value visible. The tuner reports its
verification as `reported_state`, not a hardware acknowledgement. After applying,
query again and inspect the values; a radar restart followed by Query Params is a
stronger manual persistence check. Recheck Engineering Mode afterwards.

The original no-change Apply failure has not been reproduced on physical hardware.
The new diagnostics distinguish an upfront rejection from a sent write whose
reported value did not match; pacing is a mitigation, not a confirmed diagnosis
of that particular failure.

## Local validation

The self-contained package was checked with ESPHome 2026.9.0 using two synthetic
ESP32-C3 / ESP-IDF configurations: the original GPIO4/GPIO3 pins with package
defaults, and GPIO6/GPIO7 with an existing UART ID, 115200 baud, a 2048-byte buffer,
a different presence name/delay, and unrelated Status/Restart entities. Both
configurations validated. Assertions checked the selected UART, all 18 energy and
18 threshold names, one recovery script, and preservation of unrelated entities.
C++ source generation also passed for the default configuration.

Package-independent tuner regressions also passed, including Learn/Apply on a
configuration without Query Params or the package's internal IDs. These checks do
not constitute a completed firmware build, flash, or proof of physical recovery.
