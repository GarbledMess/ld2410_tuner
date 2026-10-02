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

A supplied recovery log shows live energy/distance updates continuing between
failed parameter reads, a radar restart attempt, and the old message scheduling
another cycle. This confirms unsuccessful recovery while streaming remains
active; it does not establish that the restart was acknowledged or that recovery
caused the initial blank settings. The repeated 250 ms operation warnings match
`read_all_info()` sending five queries with a blocking 50 ms delay each. The
following Baud Rate update is locally derived, not a successful parameter reply.

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
on / 1 s off), one-second main sensor throttles and two-second gate throttles. It
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
(500ms), `ld2410_presence_off_delay` (1s), `ld2410_sensor_throttle` (1s), and
`ld2410_gate_throttle` (2s). No substitutions are needed when using the defaults.
Other custom radar names or logic need a tailored configuration; an existing
standalone ESPHome setup remains supported by the tuner without adopting this file.

### Updating existing single-radar nodes

Replace the shared `ld2410c.yaml` file and rebuild normally. Keep the existing
`packages: ld2410: !include ...` configuration, UART pins/ID, node identity, and
`ld2410_presence_*` / throttle substitutions. No new include variables or
subdevice declarations are required for existing single-radar nodes. The added
instance options are only needed when introducing more radars or explicitly
selecting a UART. The tuner also continues to support nodes still running the
previous package, without reflashing them for its Engineering Mode checks.

### Multiple LD2410Cs on one ESP

Include the **same local file once per radar**. Give each additional radar a unique
`ld2410_id` prefix, its own `ld2410_uart_id`, and a separate `ld2410_device_id` from
`esphome.devices`. These are [ESPHome subdevices](https://esphome.io/components/esphome/#sub-devices):
Home Assistant and the tuner treat each radar separately. Names such as **Presence**,
**G0 Still Energy**, **Timeout**, and **Engineering Mode** stay unchanged; the
subdevice name identifies which radar they belong to.

This example keeps the existing first radar on the main device, preserving its
identity, internal IDs, and entity names. Merge `devices` into your existing
`esphome` block; keep its name and friendly name. Use your actual UART pins.

```yaml
esphome:
  # Keep your existing name, friendly_name and other settings here.
  devices:
    - id: second_device
      name: Second Radar

uart:
  - id: radar_bus_one
    tx_pin: GPIO4
    rx_pin: GPIO3
    baud_rate: 256000
    rx_buffer_size: 1024
    parity: NONE
    stop_bits: 1
  - id: radar_bus_two
    tx_pin: GPIO6
    rx_pin: GPIO7
    baud_rate: 256000
    rx_buffer_size: 1024
    parity: NONE
    stop_bits: 1

packages:
  first_radar: !include
    file: packages/ld2410c.yaml
    vars:
      ld2410_uart_id: radar_bus_one
  second_radar: !include
    file: packages/ld2410c.yaml
    vars:
      ld2410_id: second
      ld2410_uart_id: radar_bus_two
      ld2410_device_id: second_device
```

Add another UART, subdevice and include for each further radar; there is no fixed
radar count in the package. Each radar requires a separate supported UART and
suitable pins. ESP32 variants have different UART limits, and a serial logger may
use a UART too. The example pins are for an ESP32-C3 and must not be copied onto a
different board without checking its pin assignments.

For multiple UARTs, supply their full configuration in the host's `uart:` list:
that list replaces the package's single-UART defaults. The package respects your
pins and uses the bus explicitly selected by `ld2410_uart_id`. Use unique IDs
throughout: `ld2410_id: second` reserves internal IDs starting `second_`, including
`second_radar`, so the subdevice above deliberately uses `second_device`.

Timing and reporting options can also go in an include's `vars`, for example
`ld2410_presence_on_delay: 2s`. Existing top-level timing `substitutions` continue
to work as defaults. Every radar gets independent recovery state, buttons,
thresholds and timing diagnostics. Existing single-radar includes need no changes.

For a new installation you may give every radar its own subdevice. Keep existing
radars on their current device if preserving HA entity identity and tuner history
is important; moving an existing radar into a new subdevice creates a different
HA device association. Never put multiple radars' same-named gate entities on one
HA device, as that loses the distinction the tuner needs.

This revision is validated with ESPHome **2026.9.0 and 2026.9.1**. Subdevices require Home
Assistant **2025.7 or later** ([release notes](https://www.home-assistant.io/blog/2025/07/02/release-20257)).
Updating the tuner through HACS does not replace or flash the local ESPHome package.

### Automatic Engineering Mode

The tuner checks radars with **recording enabled** on startup and every **10 minutes**. If an enabled,
unambiguous **Engineering Mode** switch reports **off**, it requests **on** so the
radar can supply per-gate energies. This works with ordinary ESPHome firmware too;
no custom package, internal ID or recovery button is required. Renamed entities
are recognized by their original Engineering Mode name. Radars with recording disabled are left alone. Existing devices without an explicit
recording setting retain their original recording-enabled default. Newly discovered
devices start with recording disabled and are not switched on until you enable recording.

The tuner waits during its own configuration writes/recovery and while the package
reports active recovery. Failed calls or missing on reports retry no sooner than 10 minutes,
then 20 minutes, then 30 minutes between later attempts, on a periodic check. A brief optimistic on
report followed by off retains a cooldown. The device's Recommendations section
shows pending or unsuccessful enabling, and failures are logged. Unknown,
unavailable, disabled, absent, or ambiguous switches are left alone.

This changes reporting mode, not gate thresholds, presence filters, timeout or
Bluetooth. Turning Engineering Mode off manually is temporary while recording is
enabled: it will try to enable it again. Reported on is an ESPHome state check, not
proof of a hardware acknowledgement.

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

A once-per-minute check starts recovery when settings are missing, unless the
previous cycle failed. **A failed cycle pauses all package recovery commands**,
including queries and radar restarts. The previous implementation retried
indefinitely with a one-minute restart cooldown; it could repeatedly interrupt
a radar whose configuration never loaded. The cooldown limited frequency, not
the number of failed attempts. The new pause prevents that repeated-command loop;
it does not establish the cause of the initial missing settings.

**LD2410 Recovery Status** shows whether settings are available, recovery is
running, or recovery paused. **Retry Radar Recovery** explicitly starts another
cycle after a failure; presses during an active cycle are ignored. The one-minute
restart cooldown still applies to manual retries. A successful settings-readiness
transition automatically re-arms recovery for a later fault, including recovery
through the Bluetooth app. This is not a once-per-boot restart limit. Restarting
the ESP starts a fresh cycle because recovery state is not persisted.

Saved thresholds and Bluetooth are not rewritten. Restoring Engineering Mode is
a best-effort command, not an acknowledgement; an unknown pre-restart mode cannot
be reconstructed. A radar restart briefly interrupts presence reports. Readiness
uses ESPHome's reported values, which can also be populated by optimistic manual
writes; it is not proof of a radar acknowledgement. The pause governs this
package only: manual controls and the tuner's separately initiated Learn recovery
can still send commands.

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

The failed-cycle pause is covered by five regression tests that replay the actual
package YAML actions: persistent failure over two hours, settings returning and a
later fault, explicit retry with the restart cooldown, successful first-query
recovery, and retry during an active cycle. This replay models automation control
flow, not serial traffic or radar responses. The updated package also passed
ESPHome 2026.9.0 configuration validation and C++ source generation with a
synthetic ESP32-C3 node using GPIO9/GPIO10 and the supplied I2C peripherals. No
firmware compilation, flash or hardware recovery was performed for this change.

If the temporary `!extend ld2410_recover_parameters` override from troubleshooting
is present in a node, remove it when installing this revision to enable the
bounded recovery routine. Leaving it present continues to disable recovery.

## Exposing timing for calibration

The package now exposes **LD2410 Presence On Delay** and **LD2410 Presence Off
Delay** as read-only diagnostic entities. Their values use the same substitutions
as the existing presence filters, so overrides remain consistent. Defaults remain
500ms on and 1s off; the old text in this guide incorrectly described the off delay
as 5s. Neither filter was changed by adding these diagnostics. They refresh every minute; Learn reads the currently available Home Assistant
states.

The tuner discovers the radar's standard **Timeout** number without this package.
Entity IDs ending in `_timeout`, or an original entity name of Timeout, are
supported, including renamed HA entities. Label-expiry timeouts are excluded.
Missing, invalid or ambiguous matches remain unknown. By default, missing values stay unknown. The optional global fallback policy
uses explicitly configured values for missing timing and marks them as assumptions.
The global disabled mode ignores all timing adjustments. Neither mode changes a
timing control when applying thresholds.

For standalone firmware, the two optional diagnostics can be added to the existing
`text_sensor:` list without importing the package:

```yaml
text_sensor:
  - platform: template
    name: LD2410 Presence On Delay
    entity_category: diagnostic
    lambda: return {"500ms"};
    update_interval: 60s
  - platform: template
    name: LD2410 Presence Off Delay
    entity_category: diagnostic
    lambda: return {"1s"};
    update_interval: 60s
```

**Use your actual filter values**, ideally shared substitutions, rather than
copying these example numbers blindly. The replay assumes `delayed_on` followed
by `delayed_off` on `has_target`. Other filters, dynamic lambdas, inverted outputs,
multiple radars on one HA device, or a different filter order require a matching
configuration; omit these diagnostics if they do not describe your setup. Numeric
sensor/number entities named Presence On Delay and Presence Off Delay also work
when they expose a duration unit (`ms`, `s`, `min`, or `h`). With only Timeout
available, learning evaluates radar hold and clearly marks the filters unknown.
With no Timeout it estimates raw-crossing durations between snapshots. Learn and Apply remain usable.

## Timing and reporting options: behavior changes to consider

No option below was changed automatically:

| Option | Effect | Tradeoff |
| --- | --- | --- |
| Radar Timeout | Holds detection through quiet gaps | Increasing it delays vacancy and prolongs false triggers. It does not reject positive spikes. |
| `ld2410_presence_on_delay` | Requires the radar's already-held output to remain on | Increasing it delays arrival detection. A short energy spike can still pass if radar Timeout holds it long enough. |
| `ld2410_presence_off_delay` | Adds a delay after the radar clears | Increasing it delays vacancy further and extends false presence. |
| `ld2410_gate_throttle` | Limits per-gate reports sent to HA | Faster reporting increases traffic and may improve freshness, but the tuner's approximately six-second recording cadence still cannot resolve sub-second events. It does not change radar sensitivity. |
| `ld2410_sensor_throttle` | Limits aggregate energy/distance reports | Faster reporting affects telemetry traffic, not the radar's gate decision or its timeout. |
| Maximum distance / distance resolution | Changes the physical area covered by active gates | Can exclude pets or clutter, but also people at those locations. Changing these requires a new assessment; it is not a general pet filter. |

Keep raw per-gate energies for calibration. Adding averaging, median filters or
clipping to them would make the learner fit a different signal from the radar's
own threshold input. Engineering Mode is needed for per-gate reporting; it is
already exposed by the package. No additional firmware filtering or custom
presence handler is introduced.

Timing semantics: [LD2410 configuration](https://esphome.io/components/sensor/ld2410/)
and [ESPHome binary sensor filters](https://esphome.io/components/binary_sensor/#binary-sensor-filters).

The timing metadata addition was also validated and passed C++ source generation
with ESPHome 2026.9.0 for both default and overridden delays (2s on / 10s off).
Generated code was checked to use the same values for the actual filters and both
diagnostics, while preserving different local UART pins. This is configuration
and code-generation validation, not a firmware build or hardware test.

## Recovery before manual or overnight learning

Version 1.12.0 checks exposed active-gate thresholds and distance limits before
fitting. Healthy devices are left alone. Missing states trigger up to two Query
Params attempts, then an exposed Radar Restart, then a Bluetooth cycle if needed
and available. Each restart is followed by parameter queries. These are optional
standard ESPHome entities; no package or particular internal firmware ID is required.
Ambiguous, disabled or other-device controls are not used. A generic ESP restart
button is never pressed. Missing threshold entities require enabling them rather
than a recovery attempt.

**Behaviour change:** Learn may briefly interrupt radar reports when recovering
missing settings. Bluetooth is restored to its original known on/off state, and
Engineering Mode is re-enabled if it was on. Unknown Bluetooth state is not guessed.
Recordings pause during recovery so those transitions do not become training data.
Recovery is bounded per Learn and has a one-minute cooldown, not a once-per-boot
limit. It does not run continuously and never automatically applies thresholds.
Apply retains its existing query-only recovery and explicit confirmation.

The device report shows progress, attempts and any failure. Pending Bluetooth
restoration is saved before toggling; the next Learn retries it first if a restart
or interruption prevented confirmation. Overnight calculation quality is separate
from job failure: a completed recommendation can have a false-active penalty.
Service calls and reported-state restoration are covered by synthetic tests;
physical radar recovery and the intermittent firmware cause remain unproven.

## Multi-radar regression checks

Run `python3 tools/validate_esphome.py` in an environment with `esphome==2026.9.1`.
CI runs the same check on ESPHome 2026.9.0 and 2026.9.1. It validates the original
plain `!include`, existing UART IDs/pins, all five timing/name/reporting substitutions,
two radars on ESP32-C3, three on ESP32, and twelve independent host UART instances
at 115200 baud. Assertions check entity names, bus/subdevice isolation, independent
timing, and recovery instance counts; each case generates C++ successfully. The
host case checks that package structure has no small fixed count, not ESP32 UART
capacity. These are schema/code-generation checks, not compiled firmware or
physical multi-radar tests.
