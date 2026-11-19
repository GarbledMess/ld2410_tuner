# LD2410 Tuner

A Home Assistant integration that records the per-gate energy of your HLK-LD2410
radars, lets you label when rooms were occupied or empty, and learns gate thresholds
(and optionally timeouts) from those labels.

- Records every gate energy change, per radar, into compact day files.
- Labels can come from you, from other entities (cameras, `input_boolean`s, template
  sensors), from [Bermuda](https://github.com/agittins/bermuda) phone/watch tracking,
  or from the tuner's own automatic guesses (down-weighted).
- Learns thresholds per radar or for a whole room of radars together, shows what each
  candidate would have missed or falsely detected, and applies them only when you say
  so (or automatically, if you opt in).
- All heavy work runs in resource-limited, low-priority background workers.

[Release notes](CHANGELOG.md) · [Documentation](docs/README.md) ·
[Issues](https://github.com/GarbledMess/ld2410_tuner/issues)

## Contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Getting started](#getting-started)
- [The panel](#the-panel)
- [Labelling](#labelling)
- [Rooms, zones and shared radars](#rooms-zones-and-shared-radars)
- [Learning and applying thresholds](#learning-and-applying-thresholds)
- [Radar timeout](#radar-timeout)
- [Automatic labels from other entities and Bermuda](#automatic-labels-from-other-entities-and-bermuda)
- [Recorded data and storage](#recorded-data-and-storage)
- [ESPHome](#esphome)
- [Limitations](#limitations)
- [Further documentation](#further-documentation)

## Requirements

- Home Assistant running on Linux (heavy jobs run in subprocesses with enforced memory
  and CPU limits; without those controls the job is refused rather than run unprotected).
  The limits follow what Home Assistant can spare: a job starts only while Home Assistant
  has memory to spare, is paused while Home Assistant's event loop is slow, and is stopped
  if memory runs short.
  The local brand icon needs Home Assistant 2026.3 or later.
- LD2410 radars on ESPHome with
  [engineering mode](https://esphome.io/components/sensor/ld2410/#switch) available and
  the per-gate energy and threshold entities exposed, named in the usual
  `*_g0_move_energy` / `*_g0_move_threshold` pattern (gates 0–8, move and still).
- Optional: the [bundled ESPHome package](examples/esphome/ld2410c.yaml), which exposes
  every entity the tuner uses and adds recovery and timing diagnostics. Existing
  configurations with the required entities work without it.

## Installation

### HACS (custom repository)

1. In HACS, open **Custom repositories** and add
   `https://github.com/GarbledMess/ld2410_tuner` with type **Integration**.
2. Download **LD2410 Tuner** and restart Home Assistant.
3. Add **LD2410 Tuner** under **Settings → Devices & services → Add integration**.
4. Fully refresh the browser to load the **LD2410 Tuner** panel.

The integration is not in the HACS default catalogue. HACS shows updates when a new
release is published; it does not install them or restart Home Assistant on its own.

### Manual

Copy `custom_components/ld2410_tuner` from this repository (or from the
`ld2410_tuner.zip` release asset) into `<config>/custom_components/ld2410_tuner`,
restart Home Assistant, then continue from step 3 above. Copy only that folder, not
the repository root.

### Upgrading

Stored labels, history and settings are kept across upgrades. Results learned by an
older model are marked and must be learned again before they can be applied.

> **Upgrading from a version before 1.24?** The first start moves recorded history out
> of `.storage/ld2410_tuner.data` into day files, and an older version cannot read them.
> Take a Home Assistant backup first. See [Storage](docs/storage.md#upgrading-from-before-124).

## Getting started

1. **Turn on recording.** Newly discovered radars start with **Record data** off.
   Switch it on for each radar you want to tune. The tuner turns on engineering mode
   for recording radars that report it off.
2. **Label empty time.** Mark the room **Empty** while it has its usual fans, curtains,
   pets and background activity.
3. **Label presence.** Mark it **Present** while people walk, sit, and sit very still in
   the places that matter. Several sessions on different days are worth far more than
   one walk past the sensor. Use **Unknown** for anything you are not sure about.
4. **Learn.** Press **Learn thresholds** on a radar (or learn a room together). Learning
   needs at least five minutes of each state.
5. **Review and apply.** Read the report in **Recommendations**, then press **Apply
   learned thresholds** if you want it. Test again in the room, especially quiet
   sitting and empty-room false triggers.

After that, keep correcting labels when the radar gets it wrong and let
[overnight learning](#overnight-learning-and-automatic-apply) keep the thresholds
current.

## The panel

From top to bottom:

- **Workers** – live status of the background workers: what is running, queue length,
  CPU and memory, the running job's limits, and estimated time left.
- **Global settings** – overnight learning schedule, automatic Apply defaults,
  learning timing and recording storage.
- **How rooms and zones work** – a short in-panel summary of [the rules
  below](#how-labels-combine).
- **Label a time period** – label many rooms, zones and radars at once.
- **Rooms and zones** – the layout tree of areas, zones and radars, with room charts,
  assessments and room-level Learn.
- **Radar cards** – one per radar, with sections **Current presence label**,
  **Automatic analysis & feedback**, **History**, **Gate visualization**,
  **Recommendations**, **Recorded data** and **Data & export**.
- **Bermuda** – phone and watch presence sources.
- **Excluded radars** – radars left out of everything, with **Include again**.

Collapsible sections start closed and stay as you left them while the panel refreshes.
The History and Recommendations shortcuts in a radar's header open those sections.
Long-running actions show a named spinner and lock conflicting controls on that radar.

## Labelling

There are three statuses everywhere: **Present**, **Empty** and **Unknown**. Unknown
excludes a period from learning, including any automatic guess for it.

- **Now:** in a radar's or room's **Current presence label**, choose a status and how
  long it should last, so a forgotten label does not run on.
- **Past periods:** open **History**, pick a day and drag across the time bar (or type
  From/To), choose a status and save. Drag the handles to adjust; focused handles
  move by one minute with the arrow keys, 15 with Shift. Dragging alone never saves.
  A range selected on the graph is labelled the same way.
- **Many rooms at once:** **Label a time period** labels any mix of rooms, zones and
  radars in one go. Each is labelled at its own level, exactly as its own chart would.
  A quiet house is best marked by its rooms; tick a radar on its own only to correct
  it. **Save this selection as** stores the ticked set under a name, shared by every browser.
  If any chosen item cannot be labelled, none is.

Charts can zoom into part of a day: **−**/**+** halve or double the window (down to ten
minutes), **‹ Earlier**/**Later ›** move it, **Zoom to selection** fits the selected
period and **Whole day** resets. Zoomed windows load finer buckets, down to six
seconds. The line is the bucket mean; the band is the recorded min–max.

Charts show separate evidence lanes for **Human (manual)**, **Entity / sensor** and
**Guessed (automatic)** labels (solid = Present, dashed = Empty, dotted = Unknown, gap
= nothing recorded). Toggle them with the buttons below the chart.

## Rooms, zones and shared radars

The **Rooms and zones** card mirrors your Home Assistant areas. Each area starts with
its recording radars; inside it you can add **zones** (for example *Desk* and *Sofa*
inside *Office*), each with its own selection of radars. Radars no zone claims appear
under **Elsewhere in <area>**. You can also make area-less manual groups. **Edit
members** overrides an area's default radars, **Restore area default** undoes it.
None of this changes Home Assistant's registry.

Zones are assessed and learned independently: someone at the desk does not count as
sofa occupancy. A radar that covers both can belong to both zones; zones sharing a
radar form a **learning set** and are learned together, so the shared radar gets one
set of thresholds.

### How labels combine

You can label a room or zone directly. For any room or zone, the first of these that
applies decides whether it was occupied:

1. A human label on that room or zone (an explicit Unknown stops the fallbacks).
2. An implied label: a Present zone makes its area Present; an Empty area makes its
   zones Empty.
3. Its member zones.
4. Automatic evidence: sensor labels, then the room estimate (which needs two members
   to say Present, or the only member of a one-radar room).

A radar's own label means "this radar should (or should not) detect here", and wins
over anything room-level for that radar. An Empty room is Empty for each of its
radars. A Present room is credited to the radars that saw it: a radar is credited when
its energy stays clearly above its own empty baseline for 30 seconds. The **Seen by**
chips on an occupied period show that attribution; tap one to correct it, which saves
a label on that radar. In a one-radar room, room labels train the radar directly.

Automatic evidence (guesses, sensors, attributions) only counts at its confidence
times the **Automatic evidence weight** setting, which defaults to 0 and at most gives
it a quarter of a human label's weight. Full rules: [How learning
works](docs/how-learning-works.md#room-truth-and-attribution).

### Room settings

Under **Assessment & room settings** on each room or zone:

- **Assess current settings** replays the last 24 hours, 3 days or 7 days with each
  radar's live thresholds and timing, and reports missed and false time for the room
  as a whole and each radar's contribution. It runs on the server and is kept until
  membership, labels, thresholds or timing change.
- **Presence priority** sets how Learn trades missed presence (the room going dark
  with someone in it) against false presence (lit while empty):

  | Choice | A missed second costs |
  | --- | --- |
  | Avoid false presence | 1× a false second |
  | Lean towards empty | 2.5× |
  | Balanced (default) | 5× |
  | Lean towards presence | 15× |
  | Never miss presence | 50× |

  Use **Never miss presence** for lights in a bathroom or on stairs; use **Avoid false
  presence** where a false detection is the expensive mistake, such as heating or an
  alarm that arms when a room is empty. A radar's own labels use the highest priority
  of the rooms it serves. Changing it marks the room's results out of date.

### Excluding a radar

**Exclude radar** on a radar's card leaves it out of recording, Learn, Apply, rooms and
assessments without touching the device in Home Assistant, for example after it was
moved behind metal. Nothing stored is deleted. **Include again** under **Excluded
radars** brings it back as it was.

## Learning and applying thresholds

**Learn thresholds** starts a server-side job: you can close the tab and come back. A
room or learning set can be learned together from the **Rooms and zones** card. The
learner:

- evaluates all enabled gates together, as the radar does: presence is detected when
  any gate is strictly above its threshold;
- scores candidates by missed occupied time against false-active empty time
  (5:1 by default, or as set by the room's presence priority), replaying the radar's
  timeout and ESPHome on/off delays where it can read them;
- sets move gates by rule (to catch arrivals) and searches still gates (to hold
  presence); a gate set to 100 is off;
- gives human labels priority over automatic evidence, and checks on later data whether
  using automatic evidence actually helps before it does.

Details: [How learning works](docs/how-learning-works.md).

### Reading the result

The **Recommendations** section shows the device-wide report, the gate table, and the
**Apply** button, coloured by quality:

- **Green** – meets the target (99.9% of occupied time detected) on recorded evidence.
- **Amber** – caveats: unknown device timing, little human evidence, or a concern from
  the chronological backtest.
- **Red** – misses the target, misses a whole occupied session, or is active
  throughout empty time.

The colour is advice only: you can still apply a red result, and a green one is not a
guarantee for the room. **Periods driving this result** lists the recorded stretches
that cost the most, each with **Review on graph** to open it in History. Usually that
is a mislabelled period (fix it, or mark it Unknown) or genuinely overlapping signals
(a pet, a fan) that no static threshold can separate.

### Saved results

**Recommendations** keeps four slots:

- **User learnt** – your latest manual Learn.
- **Autolearnt** – the latest overnight Learn.
- **Current** – the last fully applied result.
- **Previous** – the settings that Apply replaced, for rollback.

Choosing a slot updates the report, gate table, chart and Apply button together.
**Compare saved patterns** scores the slots and the live settings side by side on a
0–100 scale (`100 − 5 × missed % − false-active %`); the header shows **Current** (live)
and **Best**. Scores from different dates may use different evidence.

Apply writes only gate thresholds, at a paced rate, checks that the radar reports the
new values, and stops at the first failure. Lower thresholds are written before higher
ones are raised, so coverage is never withdrawn first. A partly written Apply is
reported as such.

### Overnight learning and automatic Apply

In **Global settings → Learning and automatic Apply**:

- **Overnight learning** learns every recording radar once a day at the chosen time
  (default 03:00, off until you enable it). A pass starts within 15 minutes of that
  time; one missed by a restart waits until the next day.
- **Global default: automatically apply better results** applies a new result only if, on the same
  fresh recordings, its weighted cost is strictly lower than the live thresholds'.
  **Default scope** is **Overnight learning only** (the default) or **Every completed
  Learn**.
- Each radar can override this under **Recommendations → Automatic Apply for this
  sensor**: **Inherit global**, **Off**, **Overnight learning only** or **Every completed
  Learn**.

Each radar header shows its latest overnight outcome; tap it for the report.

### Learning timing

**Global settings → Learning timing** chooses how missing device timing is handled:

- **Use available device timing** (default) – missing values stay unknown.
- **Use defaults where device timing is missing** – fill each missing value from the
  editable defaults (1 s timeout, 0.5 s on delay, 1 s off delay).
- **Disable timing adjustments** – score raw gate activity only.

Learn again after changing it.

## Radar timeout

A radar keeps reporting presence for its timeout after its last detection. Longer
bridges a still person's dips; it also holds every false detection longer. Every Learn
scores 1, 2, 3, 5, 8, 12, 15, 20 and 30 seconds (and the radar's current timeout) at
the live thresholds and suggests the shortest one that keeps at least **Timeout
improvement to keep** (80% by default) of the best one's improvement. In a room with
several radars, each is scored with the others in place, so where one radar already
covers the room, the others keep short timeouts. The scores appear under **Radar
timeout** in the radar's learning **Details**.

**Automatic timeouts** are off by default. Turn them on globally (overnight only or
every Learn) or per radar with **Automatic timeout for this sensor**. When on, Learn
sets the suggested timeout and the next Learn tunes thresholds for it.

## Automatic labels from other entities and Bermuda

Other sensors can label a radar's or room's time automatically. They are training
evidence at a chosen confidence (default 90%), never treated as human labels, and your
own labels always override them.

- **Entities:** in **Current presence label → Automatic labels from other entities**,
  add any entity that reports `on`/`off` or `true`/`false`: a camera's person-detected
  binary sensor, an `input_boolean`, a template sensor. Any source reporting presence
  labels Present. **Mark "Empty"** is off by default; if you turn it on, every boolean
  source must report absence. Only do that when the sources cover everyone in the room.
- **Bermuda:** in the **Bermuda** card, switch on the phones and watches to use and the
  rooms (Home Assistant areas with a radar) to use them in, optionally with a range
  limit, then **Save Bermuda settings**. Bermuda only ever marks a room Present; use
  devices people carry, not pets' tags or fixed beacons.
- **Trim start/end of presence** (default 10 s each) keeps the uncertain edges of each
  sensor-reported period out of the labels; periods of 20 s or less add nothing.

Sources are read every two seconds, so very short pulses can be missed.

## Recorded data and storage

History is stored in `<config>/.storage/ld2410_tuner/history/`, one folder per radar
and one file per day, so it is included in Home Assistant backups. Every gate energy
change is stored as received, at millisecond resolution, and nothing in between.

- **Recorded data** on a radar's card lists the stored days and their sizes, shows the
  raw rows 100 at a time, and has **Download day CSV**.
- **Data & export → Export JSON** downloads everything stored for the radar (can be
  several MB); **Export CSV** is one row of statistics and recommendations per gate.
  **Clear data** removes the radar's recorded history, labels and recommendations.
- **Global settings → Recording storage** sets retention, applied once a day:

  | Default | |
  | --- | --- |
  | 7 days | start thinning low-confidence automatic time (below 50%) |
  | 18.5 days | thinning threshold reaches 75% |
  | 30 days | remove all unlabelled and automatic time |
  | 30 days | remove human-labelled time (never earlier than automatic) |
  | 100 MiB | total size ceiling |

  Under size pressure, automatic time goes first (lowest confidence, then oldest);
  human-labelled time is only removed after that. **Trim now** trims to 90% of the
  ceiling immediately. Removed history cannot be restored without a backup or export.

If the ceiling can't be met even with all history removed, recording pauses and the
panel says why; any other storage failure leaves recording running until the retry. Details:
[Storage](docs/storage.md).

## ESPHome

Use the [complete LD2410C package](examples/esphome/ld2410c.yaml) if you want all
entities, recovery and timing diagnostics in one file: keep your node's UART pins and
include the package once per radar. The [ESPHome guide](docs/esphome-recovery.md)
covers [several radars on one ESP](docs/esphome-recovery.md#multiple-ld2410cs-on-one-esp),
settings missing after boot, Apply appearing to do nothing, and
[exposing timing without the package](docs/esphome-recovery.md#exposing-timing-for-calibration).

Every 10 minutes the tuner asks recording radars that report engineering mode off to
turn it back on ([details](docs/esphome-recovery.md#automatic-engineering-mode)).

## Limitations

- The radar still uses static per-gate thresholds. The tuner picks better ones from
  recorded evidence; it does not add a smarter presence entity.
- Scores describe what the thresholds would have done on your recordings, which are
  correlated samples about six seconds apart, not an independent field test.
- Where quiet presence and empty-room noise (pets, fans, curtains) overlap, no
  threshold separates them; the report shows the trade-off rather than hiding it.
- Unknown device timing makes results less certain. Expose timeout and delays if you
  can.
- Absent maximum-distance entities are assumed to cover gates 0–8; expose them if you
  use a reduced range.
- Camera video is never processed; only the camera's boolean entities are read.

## Further documentation

- [How learning works](docs/how-learning-works.md) – evidence, guesses, room truth,
  scoring, timing and references in detail.
- [Storage](docs/storage.md) – file format, retention, background load and the 1.24
  migration.
- [ESPHome setup and recovery](docs/esphome-recovery.md)
- [Development](docs/development.md) – repository layout, checks, CI, releases and the
  offline validator.
- [Documentation index](docs/README.md) – architecture notes, reviews and validation
  reports.
