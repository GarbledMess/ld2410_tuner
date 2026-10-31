"""Exact byte-lane threshold comparisons, bounded to one joined vector."""

from collections import OrderedDict

from ...const import HISTORY_KEYS
from .constants import MASK_CACHE_BYTES

_VALID = bytes(value if value <= 100 else 0 for value in range(256))
_ATTENUATED = bytes(3 * value // 5 if value <= 100 else 0 for value in range(256))


class Decision:
    """Fold fixed coordinate hits with C integer byte-lane arithmetic.

    Energies and thresholds are at most100. Setting each energy lane's high
    bit before subtracting threshold+1 prevents inter-lane borrowing; a high
    result bit means strict crossing in that lane. Missing/unselected energies
    map to zero. No integer spans more than this zone's 18*member byte lanes.
    """

    def __init__(self, problem, store, zone, pattern, rid, channel, attenuate):
        self.members = tuple(sorted(zone.members))
        self.problem, self.attenuate = problem, attenuate
        self.position = self.members.index(rid) * 18 + channel if rid in self.members else None
        thresholds = bytes(pattern[q].get(c, 100) + 1 for q in self.members for c in range(18))
        self.thresholds = int.from_bytes(thresholds, "little")
        self.high = int.from_bytes(bytes([128]) * len(thresholds), "little")
        bit = 128 << (8 * self.position) if self.position is not None else 0
        self.fixed = self.high ^ bit
        if not hasattr(store, "_decision_vectors"):
            store._decision_vectors = OrderedDict()
        self.cache = store._decision_vectors

    def energies(self, vector, state, source):
        key = self.members, vector, state == "present", source == "human", self.attenuate
        if key not in self.cache:
            self.cache[key] = self._transform(vector, state, source)
            # Account for input/output bytes plus conservative Python key overhead.
            limit = max(1, MASK_CACHE_BYTES // (2 * len(vector) + 256))
            while len(self.cache) > limit:
                self.cache.popitem(last=False)
        self.cache.move_to_end(key)
        return self.cache[key]

    def _transform(self, vector, state, source):
        if state != "present":
            return vector.translate(_VALID)
        raw = vector
        if source != "human" and self.problem.policy.quality.get("models"):
            raw = self._supported(raw)
        return raw.translate(_ATTENUATED if self.attenuate else _VALID)

    def _supported(self, vector):
        raw = bytearray(vector)
        models = self.problem.policy.quality.get("models", {})
        for i, rid in enumerate(self.members):
            model = models.get(rid)
            if model is not None:
                _support_radar(raw, i * 18, model)
        return bytes(raw)

    def crossing(self, vector, state, source):
        values = self.energies(vector, state, source)
        lanes = (int.from_bytes(values, "little") | self.high) - self.thresholds
        if lanes & self.fixed:
            return 101
        return values[self.position] if self.position is not None else -1


def _support_radar(raw, offset, model):
    for channel, key in enumerate(HISTORY_KEYS):
        at = offset + channel
        if not model.supports(key, raw[at]):
            raw[at] = 0
