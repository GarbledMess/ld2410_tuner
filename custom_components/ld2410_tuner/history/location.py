"""Where recorded history lives: Home Assistant's ``.storage`` folder (the default) or a folder
the user chose, such as a USB drive, an SD card or a network share mounted into Home Assistant.

A chosen folder holds ``<folder>/ld2410_tuner/history`` (the day files) beside a marker file.
The marker is what tells a mounted drive from the empty mount point left when it is not
mounted: without it nothing is written, so recordings never fill the system disk by accident.
The labels, settings and results stay in Home Assistant's JSON store; only history moves.

Changing the folder in the integration's options reloads it. Before recording starts, the
history is copied (each file synced to disk and its size checked), the JSON store is saved with
the new location, and only then is the old copy removed. Until that save the old copy is the
one in use, so an interrupted move starts again from it.
"""

import json
import logging
import os
import shutil
from pathlib import Path

from ..const import DOMAIN

OPTION = "history_location"
MOVE_OPTION = "move_history"
DATA_KEY = "history_location"  # In the JSON store: where the history is now.
FOLDER = DOMAIN
MARKER = ".ld2410_tuner_location"
MOVING = "history.moving"
_LOGGER = logging.getLogger(__name__)


class LocationError(ValueError):
    """A folder that cannot hold history; ``reason`` is the options form's error key."""

    def __init__(self, reason, path=""):
        super().__init__(f"{reason}: {path}")
        self.reason = reason


class LocationUnavailable(OSError):
    """The chosen folder is not there: a drive not mounted or a share that is offline."""


def normalize(location):
    """The option as stored: '' for Home Assistant's storage, else an absolute path."""
    location = (location or "").strip()
    if not location:
        return ""
    path = Path(location)
    if not path.is_absolute():
        raise LocationError("not_absolute", location)
    return os.path.normpath(path)


def parent(hass, location):
    if not location:
        return Path(hass.config.path(".storage", DOMAIN))
    return Path(location) / FOLDER


def root(hass, location):
    return parent(hass, location) / "history"


def marker(hass, location):
    """The marker a chosen folder must hold before anything is written; None by default."""
    return parent(hass, location) / MARKER if location else None


def require(store):
    """Refuse to write while a chosen folder is missing (executor: this touches the disk)."""
    found = getattr(store, "marker", None)
    if found is not None and not Path(found).is_file():
        raise LocationUnavailable(
            f"LD2410 Tuner history folder {Path(found).parent} is not available; "
            "check that the drive or network share is mounted"
        )


def check(hass, location):
    """Validate a folder for the options form (executor). Returns the normalized option."""
    location = normalize(location)
    if not location:
        return location
    chosen = Path(location)
    storage = Path(hass.config.path(".storage")).resolve()
    if chosen.resolve() == storage or storage in chosen.resolve().parents:
        raise LocationError("inside_storage", location)
    if not chosen.is_dir():
        raise LocationError("missing", location)
    try:
        _probe(parent(hass, location))
    except OSError as error:
        raise LocationError("not_writable", location) from error
    return location


def _probe(folder):
    """Create, sync, rename and remove a file: what recording needs from the filesystem."""
    folder.mkdir(exist_ok=True)
    first, second = folder / ".ld2410_probe", folder / ".ld2410_probe.renamed"
    try:
        _write_synced(first, b"ok")
        os.replace(first, second)
    finally:
        first.unlink(missing_ok=True)
        second.unlink(missing_ok=True)


def _write_synced(path, data):
    with path.open("wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def _read_marker(path):
    try:
        return json.loads(path.read_text(encoding="utf8"))
    except (OSError, ValueError):
        return None


def _mark(hass, location, moved_from=None):
    path = parent(hass, location) / MARKER
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_synced(path, json.dumps({"moved_from": moved_from}).encode())


def _available(hass, location):
    """A chosen folder in use must hold its marker; Home Assistant's storage always exists."""
    if location and not (parent(hass, location) / MARKER).is_file():
        raise LocationUnavailable(
            f"LD2410 Tuner history folder {parent(hass, location)} is not available; "
            "check that the drive or network share is mounted"
        )


def _files(source):
    """Regular files under the history root (radar folders are one level deep)."""
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"History holds a link, which is not moved: {path}")
        if path.is_file():
            yield path


def _copy(source, target):
    """Copy the tree with each file synced, then check every size; raise on any mismatch."""
    files = list(_files(source)) if source.exists() else []
    needed = sum(path.stat().st_size for path in files)
    target.mkdir(parents=True)
    if needed > shutil.disk_usage(target).free:
        raise OSError(f"Moving LD2410 Tuner history needs {needed} bytes of free space")
    for path in files:
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with path.open("rb") as incoming, destination.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if destination.stat().st_size != path.stat().st_size:
            raise OSError(f"Copy of {path} is incomplete")


def _nested(first, second):
    return first == second or first in second.parents or second in first.parents


def move(hass, current, wanted):
    """Copy history from ``current`` to ``wanted`` (executor). The old copy stays in use, and
    in place, until ``finish``: an interruption anywhere here leaves it untouched."""
    from ..worker.startup import recover_publication

    _available(hass, current)
    source, target = root(hass, current), root(hass, wanted)
    if _nested(source.resolve(), target.resolve()):
        raise LocationError("nested", str(target))
    recover_publication(source)  # Settles an interrupted startup swap before copying.
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / MOVING
    shutil.rmtree(staging, ignore_errors=True)  # A copy that was interrupted.
    found = _read_marker(target.parent / MARKER)
    if target.exists() and found and found.get("moved_from") == str(source):
        shutil.rmtree(target)  # Copied before, but the move never finished: copy afresh.
    elif target.exists() and any(target.iterdir()):
        raise LocationError("occupied", str(target))
    try:
        _copy(source, staging)
        if target.exists():
            target.rmdir()
        staging.replace(target)
        _mark(hass, wanted, moved_from=str(source))
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def finish(hass, current):
    """Remove the old copy after the JSON store points at the new one (executor)."""
    from ..worker.startup import remove_private_trees

    folder = parent(hass, current)
    shutil.rmtree(folder / "history", ignore_errors=True)
    remove_private_trees(folder)
    if current:
        (folder / MARKER).unlink(missing_ok=True)
        try:
            folder.rmdir()  # Only if nothing else was kept there.
        except OSError:
            pass


def adopt(hass, wanted):
    """Start using ``wanted`` without moving anything (executor)."""
    if not wanted:
        return
    if not Path(wanted).is_dir():
        raise LocationUnavailable(f"LD2410 Tuner history folder {wanted} does not exist")
    _mark(hass, wanted)


async def activate(hass, store, data, options):
    """Settle where history lives before the day store opens; returns ``(root, marker)``.

    ``options`` are the config entry's options (empty for the default location). A change
    that cannot be made is logged and history stays where it is; a folder in use that is not
    mounted raises ``LocationUnavailable``, and nothing is recorded until it is back."""
    current = data.get(DATA_KEY, "")
    try:
        wanted = normalize(options.get(OPTION, ""))
    except LocationError:
        wanted = current
    if wanted != current:
        try:
            await _change(hass, store, data, current, wanted, options.get(MOVE_OPTION, True))
            current = wanted
        except (LocationError, OSError, ValueError) as error:
            _LOGGER.error(
                "LD2410 Tuner history stays in %s: it could not be moved to %s (%s)",
                parent(hass, current),
                parent(hass, wanted),
                error,
            )
    await hass.async_add_executor_job(_available, hass, current)
    return root(hass, current), marker(hass, current)


async def _change(hass, store, data, current, wanted, moving):
    if moving:
        await hass.async_add_executor_job(move, hass, current, wanted)
    else:
        await hass.async_add_executor_job(adopt, hass, wanted)
    data[DATA_KEY] = wanted
    try:
        await store.async_save(data)
    except BaseException:
        data[DATA_KEY] = current
        raise
    if moving:
        await hass.async_add_executor_job(finish, hass, current)
    _LOGGER.info("LD2410 Tuner history is now stored in %s", parent(hass, wanted))
