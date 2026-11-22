# Storage

How recorded history is stored, kept and trimmed. See the
[README](../README.md#recorded-data-and-storage) for the panel controls.

## Files

By default history lives beside Home Assistant's JSON store, so backups include it (see
[Storage location](#storage-location) to keep it elsewhere):

```text
<config>/.storage/ld2410_tuner.data                                            labels, settings, results
<config>/.storage/ld2410_tuner/history/<radar device id>/<YYYY-MM-DD>.log      today (UTC)
<config>/.storage/ld2410_tuner/history/<radar device id>/<YYYY-MM-DD>.day      finished days
<config>/.storage/ld2410_tuner/history/<radar device id>/<YYYY-MM-DD>.sum      optional index
```

- **What is stored.** Every gate energy change at the time Home Assistant delivered it
  (millisecond resolution), and nothing else: no snapshots or heartbeats. A reading holds
  until the next change; changes in the same millisecond are one record. Values are
  stored as received; rounding is up to the ESPHome configuration. The radar's automatic
  guesses are stored in the same day as **guess runs** (one record per stretch with the
  same state, source and confidence step).
- **Coverage.** Recording starts with the radar's full current readings and lasts until
  an explicit end: recording paused, the radar removed, storage blocked, a
  configuration recovery or the integration stopping. An unavailable or non-numeric
  entity makes only that gate missing (missing is never zero). After a crash, coverage
  ends at the last stored change. The two-second poll only records values that state
  events missed.
- **Today's file** is append-only. Changes are buffered in memory and appended every few
  seconds and when the integration stops; a partly written last record is ignored.
- **Finished days** are compacted once into a zlib-compressed columnar file (one byte per
  gate per change, or a float column for a gate that reported non-integer values). They
  change only when retention cuts time out of them.
- **Summary files** make label counts and reference rebuilds fast. They hold no history of
  their own, are limited to 64 MiB, are rebuilt when missing or damaged, and are the first
  thing removed under size pressure.

At startup, leftovers of an interrupted day finish are removed, the private working folders
of storage transactions that never finished (`.storage/ld2410_tuner/.ld2410-storage-*`) are
deleted, and earlier days' logs are finished. Until then, recovery files from an
interrupted storage transaction are listed separately in **Recorded data**.

## Storage location

History can live outside Home Assistant's `.storage` folder, for example on a USB drive,
an SD card or a network share, to spare the system disk or keep more history. Labels,
settings and saved results always stay in `.storage/ld2410_tuner.data`; exports and the
panel's files stay there too.

1. Make the folder visible to Home Assistant:
   - **Home Assistant OS / Supervised, network share:** **Settings → System → Storage →
     Add network storage**, usage **Share** or **Media**. It appears as `/share/<name>` or
     `/media/<name>`.
   - **Home Assistant OS, USB drive or SD card reader:** recent versions mount USB storage
     under `/media/<label>`.
   - **Container or Core:** mount the drive or share on the host and bind-mount it into the
     container; use the path as Home Assistant sees it.
2. Open **Settings → Devices & services → LD2410 Tuner → Configure**, enter the folder's
   absolute path and keep **Move the existing history** on. Leave the path empty to go
   back to Home Assistant's storage. The form checks that the folder exists and that files
   can be created, synced, renamed and removed in it.
3. Saving reloads the integration. Before recording starts again, the history is copied
   into `<folder>/ld2410_tuner/history/` (each file synced, then its size checked), the JSON
   store is saved with the new location, and only then is the old copy removed. If the move
   fails (not enough space, the target already holds other history), the log says why and
   history stays where it was. An interrupted move starts again from the old copy.

With **Move the existing history** off, the tuner starts a new history in the folder (or
continues one already there) and leaves the current files where they are.

```text
<folder>/ld2410_tuner/.ld2410_tuner_location                       marker
<folder>/ld2410_tuner/history/<radar device id>/<YYYY-MM-DD>.day   same layout as above
```

- **Drive not mounted.** The marker file tells a mounted drive from the empty mount point
  left when it is not. Without the marker nothing is written there, so history never fills
  the system disk by accident. At startup the tuner waits, checking every minute, and logs
  once; the panel appears when the folder is back. If the drive goes away while running,
  readings are kept in memory and saving is retried, as for any other disk error, until it
  returns (after 15 minutes the log warns that readings are only in memory, and a restart
  meanwhile loses them).
- **Backups.** Home Assistant backups include the history only if they include its
  folder. Network-storage mounts and external drives are usually not part of a backup;
  use **Export JSON** or the drive's own backup for anything you want to keep.
- **Filesystem.** Any Linux filesystem that supports renaming files works, including
  FAT/exFAT USB sticks, NFS and SMB shares. Hard links are used where available and
  copies otherwise. A slow share makes saves and cleanups slower, not unsafe.
- **Size ceiling.** The ceiling in **Recording storage** still applies (it counts the
  history plus the JSON store); raise it if the new location has room for more.

## Recording

**Record data** is per radar. New radars start off; existing ones keep recording until
paused. Pausing stops history, histogram updates and automatic training evidence, closes
the current live label and skips the radar overnight. Existing recordings stay
available. Re-enabling starts with an Unknown label.

## Retention

Once a day (checked hourly and at startup; the last run time is saved, so a restart does
not repeat it), the retention policy in **Global settings → Recording storage** is
applied in the background. Thinning removes whole time ranges from finished days, leaving
an explicit gap; it never removes single changes and never touches today's file. Human
labels decide which rule applies to a stretch; otherwise its guess run and confidence do.
A large store is cleaned a batch of days at a time while radars keep recording. A failed
cleanup is retried after an hour, then two, doubling up to a day, so a failure never skips
more than one daily cleanup.

| Setting | Default |
| --- | --- |
| Start thinning automatic time | after 7 days, below 50% confidence |
| Minimum confidence rises linearly to | 75% at 18.5 days |
| Remove all automatic and unlabelled time | at 30 days |
| Remove human-labelled time | at 30 days (never earlier than automatic) |
| Total size ceiling | 100 MiB |

Under size pressure, automatic and unlabelled time is removed across all radars first,
lowest confidence and then oldest first; human-labelled time is removed only after that,
oldest first. Size pressure can remove data before its age expiry. Labels older than the
human retention expire with the time they cover.

**Save settings and clean now** applies the policy immediately. **Trim now** trims to 90%
of the ceiling (or a size you enter) without changing the ceiling. Both ask for
confirmation and report how much automatic and human-labelled time was removed. Removed
history cannot be restored without an export or backup. Saved results are kept, but
their accuracy reports may become stale.

### The size ceiling

The size counted is the history folder (day files, today's log and summaries) plus the
JSON store. The panel shows the size at the last save. Exports, backups and temporary
copies are outside it. Every save checks the budget. A requested limit smaller than the
protected metadata is rejected. A save that finds the store over the ceiling trims it to
90% of the ceiling, so the recordings that follow fit again; that headroom comes from
automatic time only, and human-labelled time goes only as far as the ceiling itself needs.
If an existing limit becomes impossible, recording pauses and the panel explains how to
raise it or clear unused data; no oversized file is written. That is the only storage
failure that pauses recording: after any other (a worker timing out, a disk error),
recording continues, briefly over the ceiling, until the retry trims it. Raising only the
ceiling takes effect even if the cleanup after it fails.

Label edits, or a change to a day being rewritten, make cleanup retry rather than
overwrite new evidence; recording within today's file does not, since cleanup never
touches it. A label edit, thinning or trim recounts only the time that changed.

## Background load

Recording appends changed readings to today's log every few seconds and saves the
integration's state every five minutes. Live reference evidence is folded once a minute
per radar, with radars taking turns. Heavy jobs (cleanup, label recounts after a restart,
reference rebuilds, learning) run in low-priority subprocesses with memory, CPU-time and
wall-time limits and rest between steps, using at most about half of one CPU core. On a
dozen radars, steady recording costs well under 1% of one core on a desktop. History
preparation at startup runs after setup returns, so it does not delay Home Assistant's
start; the panel appears and recording starts when it finishes.

## Exports

- **Export JSON** holds the radar's settings, statistics, learning results and every
  recorded day (lossless; weeks of history can be several megabytes). Home Assistant
  writes it and keeps it for 10 minutes, the life of its download link.
- **Download day CSV** writes every row of one day with move and still values for all
  nine gates (missing values are empty cells), from one snapshot of the day. If the day
  is rewritten meanwhile, the export stops and asks you to reload. Cancelling, or leaving
  the panel, downloads nothing.

## Upgrading from before 1.24

Version 1.24 moved recorded history out of `.storage/ld2410_tuner.data` into the day
files above, and this cannot be undone by installing an older version.

- The first start after upgrading converts every stored history block and guess run into
  day files, then saves the JSON store without them. Labels, room labels, settings,
  references and saved results stay in the JSON store.
- The conversion keeps each reading at its own time (old recordings keep their six-second
  resolution). If it is interrupted, the next start converts again from the untouched
  JSON store. Old summaries without timestamps are dropped and the affected results are
  marked for relearning.
- **Downgrading loses recordings:** an older version does not read day files, and
  upgrading again replaces the day files of each radar it converts.
- **Back up first.** To downgrade, stop Home Assistant, restore the backed-up
  `.storage/ld2410_tuner.data`, delete `.storage/ld2410_tuner/`, then install the older
  version.

Old Bermuda assignments convert automatically to area rooms; where several map to one
room, the largest range wins and any unlimited one makes it unlimited. Targets with no
matching area are kept inactive in `bermuda_migration_conflicts` in the integration data.
