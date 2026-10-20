"""Home Assistant independent display calculations and bounded export validation."""

import base64

from ..history import dayfile
from . import codec

EXPORT_BYTES = 1024 * 1024
EXPORT_NODES = 32768


def calculate(operation, view, parameters):
    """Run display jobs using the same detached day store as numerical work."""
    if operation == "charts":
        from ..presentation.charts import history_series_multi

        return history_series_multi(view, **parameters)
    if operation == "export_days":
        return recorded_days(view, parameters["device_id"])
    raise ValueError("Unsupported display operation")


def recorded_days(runtime, device_id):
    """Encode lossless days inside the child; reject exports exceeding its result cap."""
    if runtime._days is None:
        return {}
    found, size = {}, 0
    for date in runtime._days.days(device_id):
        day = runtime._days.day(device_id, date)
        if day is None:
            continue
        encoded = dayfile.encode_day(day)
        size += 4 * ((len(encoded) + 2) // 3) + 32
        if size > EXPORT_BYTES:
            raise ValueError("Export day data exceeds byte budget")
        found[date.isoformat()] = base64.b64encode(encoded).decode("ascii")
    return found


def check_export(value):
    """Bound source values before metadata helpers copy or summarize their contents."""
    codec.validate(value, max_nodes=EXPORT_NODES, max_bytes=EXPORT_BYTES)
