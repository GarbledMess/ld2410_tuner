"""A recorded day as CSV, written once by the server, as the panel used to build it.

The text is what the panel's page-by-page export produced (``static/panel/recorded_csv.js``
until 1.34.5): ISO UTC timestamps of each reading's start and end rounded to the millisecond as
JavaScript's ``Math.round`` does, the duration between those milliseconds, the 18 gate values
as JavaScript prints numbers (missing values empty) and CRLF line ends.
"""

import datetime
import math
from bisect import bisect_right
from decimal import Decimal
from functools import partial

from ..const import HISTORY_KEYS
from ..history import pace
from . import export_files, recordings

HEADER = ",".join(["start_utc", "end_utc", "duration_seconds", *HISTORY_KEYS]) + "\r\n"
BLOCK_ROWS = 8192
_EPOCH = datetime.date(1970, 1, 1)
_DAY_MS = 86_400_000
# A byte column holds a whole percentage, 255 where the gate did not read.
_BYTE_TEXT = [str(value) for value in range(255)] + [""]


def js_number(value):
    """``String(value)`` in JavaScript for a finite number."""
    if isinstance(value, int):
        return str(value)
    if value == 0:
        return "0"
    magnitude = abs(value)
    if value.is_integer() and magnitude < 1e21:
        return str(int(value))
    if 1e-4 <= magnitude < 1e16:  # Python and JavaScript print these digits alike.
        return repr(value)
    return ("-" if value < 0 else "") + _js_digits(magnitude)


def _js_digits(magnitude):
    """ECMAScript Number::toString of a positive number from its shortest digits."""
    _sign, digits, exponent = Decimal(repr(magnitude)).as_tuple()
    text = "".join(map(str, digits)).rstrip("0")
    exponent += len(digits) - len(text)
    k, n = len(text), exponent + len(text)
    if k <= n <= 21:
        return text + "0" * (n - k)
    if 0 < n <= 21:
        return text[:n] + "." + text[n:]
    if -6 < n <= 0:
        return "0." + "0" * -n + text
    power = n - 1
    mantissa = text if k == 1 else text[0] + "." + text[1:]
    return f"{mantissa}e{'+' if power >= 0 else '-'}{abs(power)}"


def _js_ms(seconds):
    """``Math.round(seconds * 1000)``: halves round up, not to even."""
    scaled = seconds * 1000
    whole = math.floor(scaled)
    return whole + 1 if scaled - whole >= 0.5 else whole


def _iso(ms):
    days, within = divmod(ms, _DAY_MS)
    date = _EPOCH + datetime.timedelta(days=days)
    seconds, millis = divmod(within, 1000)
    minutes, second = divmod(seconds, 60)
    hour, minute = divmod(minutes, 60)
    return f"{date.isoformat()}T{hour:02d}:{minute:02d}:{second:02d}.{millis:03d}Z"


def _value(value):
    return "" if value is None else js_number(value)


def _lines(starts, ends, values):
    """CSV lines for rows given as start times, end times and per-row gate value texts."""
    lines = []
    for start, end, texts in zip(starts, ends, values, strict=True):
        first, last = _js_ms(start), _js_ms(end)
        span = last - first
        lines.append(
            f"{_iso(first)},{_iso(last)},{span // 1000}.{span % 1000:03d},{','.join(texts)}\r\n"
        )
    return lines


def write_rows(out, rows, block=BLOCK_ROWS):
    """Write ``[start, end, *18 values]`` rows (None for a missing value)."""
    rows = list(rows)
    for at in range(0, len(rows), block):
        part = rows[at : at + block]
        out.write(
            "".join(
                _lines(
                    [row[0] for row in part],
                    [row[1] for row in part],
                    [[_value(value) for value in row[2:]] for row in part],
                )
            )
        )


def write_day(out, day, end):
    """Write ``day``'s readings that start by ``end`` (each ending by ``end`` at the latest),
    a block at a time; returns the number of rows."""
    total = bisect_right(day.times, end)
    for at in range(0, total, BLOCK_ROWS):
        pace.rest()
        stop = min(total, at + BLOCK_ROWS)
        texts = [_column_texts(column[at:stop]) for column in day.columns]
        ends = [min(end, value) for value in day.ends[at:stop]]
        out.write("".join(_lines(day.times[at:stop], ends, zip(*texts, strict=True))))
    return total


def _column_texts(column):
    if isinstance(column, (bytes, bytearray)):
        return list(map(_BYTE_TEXT.__getitem__, column))
    return ["" if not math.isfinite(value) else js_number(value) for value in column]


async def async_export(runtime, device_id, date, end=None, revision=None):
    """The day's CSV as a file and its download link: one pass over the day, in the
    background lane, instead of the panel fetching it in worker pages."""
    from .snapshots import _export_slot

    store = recordings._device(runtime, device_id)
    recordings.day_bounds(date)
    end = recordings._cutoff(store, device_id, date, end)
    current = store.rewrites(device_id)
    if revision is not None and (type(revision) is not int or revision != current):
        raise ValueError("Recordings changed; reload the day")
    filename = f"ld2410-{export_files.safe(device_id)}-{date}.csv"
    found = {}
    write = partial(_write, store=store, device_id=device_id, date=date, end=end, found=found)
    async with _export_slot(runtime):
        result = await export_files.publish(runtime, filename, ".csv", write)
        if store.rewrites(device_id) != current:
            await export_files.discard(runtime)
            raise ValueError("Recordings changed; reload the day")
    return {**result, "rows": found["rows"]}


@pace.background
def _write(path, *, store, device_id, date, end, found):
    day = store.day(device_id, datetime.date.fromisoformat(date), until=end)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as out:
            out.write(HEADER)
            found["rows"] = 0 if day is None else write_day(out, day, end)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path.stat().st_size
