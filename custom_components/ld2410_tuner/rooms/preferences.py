"""Preference evidence as indices and elapsed weights over shared reading columns."""

from array import array


class PreferenceRows:
    def __init__(
        self,
        rows,
        state="unknown",
        *,
        indices=None,
        weights=None,
        confidences=None,
        gate=None,
        allowed=None,
    ):
        self.rows, self.state = rows, state
        self.indices = array("I") if indices is None else indices
        self.weights = array("d") if weights is None else weights
        self.confidences = array("d") if confidences is None else confidences
        self.gate, self.allowed = gate, allowed

    def __len__(self):
        return len(self.indices) if self.allowed is None else sum(1 for _ in self._items())

    def _items(self):
        column = self.rows.column(self.gate) if self.gate in self.rows.layout else ()
        return (
            (index, weight, confidence)
            for index, weight, confidence in zip(
                self.indices, self.weights, self.confidences, strict=True
            )
            if self.allowed is None or (column and column[index] in self.allowed)
        )

    def __iter__(self):
        for index, weight, confidence in self._items():
            timestamp, values, _ = self.rows[index]
            yield timestamp, values, self.state, confidence, weight

    def add(self, index, weight, confidence=1.0):
        self.indices.append(index)
        self.weights.append(weight)
        self.confidences.append(confidence)

    def restricted(self, key, allowed):
        return PreferenceRows(
            self.rows,
            self.state,
            indices=self.indices,
            weights=self.weights,
            confidences=self.confidences,
            gate=key,
            allowed=allowed,
        )

    def readings(self, key):
        if key not in self.rows.layout:
            return []
        column = self.rows.column(key)
        return sorted(
            (column[index], weight) for index, weight, _ in self._items() if column[index] <= 100
        )
