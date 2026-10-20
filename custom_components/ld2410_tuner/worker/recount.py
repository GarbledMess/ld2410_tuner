"""Cold labelled counts from detached worker days, without parent object identities."""

import datetime

from ..history import label_counts, label_lookup
from .history import HistoryRejected


def calculate(view, device_id, cutoff, index, dates, live_date):
    read = label_lookup.index_reader(index)

    def counted(date):
        counts = label_counts.Counts(index, cutoff)
        summary = view._days.summary(
            device_id,
            datetime.date.fromisoformat(date),
            until=getattr(view, "_history_until", None),
        )
        if summary is not None:
            counts.count(summary, read, cutoff, 1)
        return counts.seconds

    seconds, inert = label_counts.empty(), []
    for date in dates:
        try:
            label_counts._combine(seconds, counted(date))
        except HistoryRejected:
            raise
        except (TypeError, ValueError, KeyError):
            inert.append(date)
    return {
        "seconds": seconds,
        "inert": inert,
        "live": None if live_date is None else counted(live_date),
    }
