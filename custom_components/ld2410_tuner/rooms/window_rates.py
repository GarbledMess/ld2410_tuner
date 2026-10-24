"""One radar's rates from its windows: no union of spans, and one window at a time.

A radar's windows are apart in time and its spans for a hit mask are each window's spans in
turn, so integrating the room's masses over them is the sum of each window's integrals. Each
window's integrals are kept for its hits, and a mask that changed in a few windows is rated
from the last mask through those windows alone. The sums are exact fixed-point integers, so a
rate is the same whichever masks came before it.
"""

from bisect import bisect_right

from ..calibration.intervals import across

# The masses integrated, in the order of each window's integrals.
MASSES = (
    ("human", "present"),
    ("automatic", "present"),
    ("human", "not_present"),
    ("automatic", "not_present"),
)
SCALE = 1 << 60  # Fixed-point unit of an integral: far finer than any recorded time.
ZERO = (0, 0, 0, 0)
LIMIT = 1 << 17  # Window integrals kept; when full everything is forgotten and learnt again.


def suited(objective):
    """Whether ``objective`` rates one radar whose windows come in time order."""
    if len(objective.radars) != 1:
        return False
    (radar,) = objective.radars.values()
    return radar.projector.ordered and radar.projector.kinds == ("lower", "upper")


class WindowRates:
    def __init__(self, objective, radar):
        self.objective, self.projector = objective, radar.projector
        self.offsets = [frame[0] for frame in self.projector.frames]
        self.ends = [
            offset + width
            for offset, width in zip(self.offsets, self.projector.widths, strict=True)
        ]
        self.masks = [frame[1].mask for frame in self.projector.frames]
        self.mass = None  # The objective's masses the kept integrals are of.

    def rates(self, bits, automatic_bits):
        """``{source: (missed, false-active)}`` for the radar's hit masks."""
        self._follow(self.objective.mass)
        detected = self._sums("detected", bits)
        automatic = detected if automatic_bits == bits else self._sums("automatic", automatic_bits)
        sums = dict(zip(MASSES, (detected[0], automatic[1], detected[2], detected[3]), strict=True))
        totals = self.objective.totals
        shares = {key: sums[key] / SCALE / totals[key] if totals[key] else None for key in MASSES}
        return {
            source: (
                0 if shares[source, "present"] is None else 1 - shares[source, "present"],
                shares[source, "not_present"] or 0,
            )
            for source in ("human", "automatic")
        }

    def _follow(self, mass):
        """Integrate the objective's current masses (a device objective scales them once
        after it is made); integrals of earlier masses are forgotten."""
        if mass is not self.mass:
            self.mass = mass
            self.present = [mass[key] for key in MASSES[:2]]
            self.empty = [mass[key] for key in MASSES[2:]]
            self.known, self.last = {}, {}

    def _sums(self, kind, bits):
        previous = self.last.get(kind)
        changed = None if previous is None else self._changed(bits ^ previous[0])
        if changed is None or len(changed) * 3 > len(self.offsets):
            sums = self._full(bits)
        else:
            old_bits, sums = previous[0], list(previous[1])
            for index in changed:
                new = self._integrals(index, self._hits(bits, index))
                old = self._integrals(index, self._hits(old_bits, index))
                for slot in range(4):
                    sums[slot] += new[slot] - old[slot]
        self.last[kind] = bits, sums
        return sums

    def _full(self, bits):
        sums = [0, 0, 0, 0]
        for index, hits in self.projector._hits(bits):
            for slot, value in enumerate(self._integrals(index, hits)):
                sums[slot] += value
        return sums

    def _hits(self, bits, index):
        return (bits >> self.offsets[index]) & self.masks[index]

    def _changed(self, difference):
        """The windows holding a bit of ``difference``, found from its set bits alone."""
        digits = format(difference, "b") if difference else ""
        size, found = len(digits), []
        position = digits.find("1")
        while position != -1:
            index = bisect_right(self.offsets, size - 1 - position) - 1
            if index < 0:
                break
            inside = size - 1 - position < self.ends[index]
            if inside:
                found.append(index)
            # Continue below this window, or with it after a bit between windows.
            position = digits.find(
                "1", size - (self.offsets[index] if inside else self.ends[index])
            )
        return found

    def _integrals(self, index, hits):
        if not hits:
            return ZERO
        found = self.known.get((index, hits))
        if found is None:
            (low, high), (false_low, false_high) = self.projector._window(index, hits)
            present = across(self.present, low, high) if low else (0.0, 0.0)
            empty = across(self.empty, false_low, false_high) if false_low else (0.0, 0.0)
            if len(self.known) >= LIMIT:
                self.known.clear()
            found = self.known[index, hits] = tuple(
                round(value * SCALE) for value in (*present, *empty)
            )
        return found
