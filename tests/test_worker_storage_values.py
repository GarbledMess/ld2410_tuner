"""Storage transactions accept the same inert metadata shapes as HA JSON storage."""

import json
import unittest

import pytest
import test_tuner as harness
from tuner_under_test.worker import protocol, transactions


def test_storage_message_accepts_realistic_learned_metadata():
    learned = {
        "devices": {
            "radar": {
                "last_learning": {
                    "proposed": {0: 15, 1: 28},
                    "settings_signature": ("motion", (25, 30), False),
                    "periods": [(100.25, 150.75)],
                },
                "learning_curve": {0.5: (1, 2), None: False, True: "yes"},
            }
        }
    }
    raw = transactions._message("learn", learned)
    assert protocol.unpack(raw, "learn", max_bytes=transactions.MESSAGE_BYTES) == json.loads(
        json.dumps(learned)
    )
    assert type(learned["devices"]["radar"]["last_learning"]["settings_signature"]) is tuple


def test_generic_protocol_still_rejects_storage_only_shapes():
    with pytest.raises(ValueError):
        protocol.pack("learn", {"tuple": (1, 2)})
    with pytest.raises(ValueError):
        protocol.pack("learn", {1: "value"})


@pytest.mark.parametrize("value", [{1: "a", "1": "b"}, {None: "a", "null": "b"}])
def test_storage_values_refuse_colliding_json_keys(value):
    from tuner_under_test.worker import storage_values

    with pytest.raises(ValueError, match="collision"):
        storage_values.normalize(value)


@pytest.mark.parametrize("value", [{(1, 2): "bad"}, {"bad": object()}, {float("nan"): 1}])
def test_storage_values_refuse_unsupported_metadata(value):
    from tuner_under_test.worker import storage_values

    with pytest.raises(ValueError):
        storage_values.normalize(value)


def test_storage_values_enforce_source_budgets_before_normalizing():
    from tuner_under_test.worker import storage_values

    with pytest.raises(ValueError, match="node"):
        storage_values.normalize([0] * 10, max_nodes=5)
    with pytest.raises(ValueError, match="string"):
        storage_values.normalize(["abc", "def"], max_bytes=5)


class LearnedStorageTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    configuration = harness.RuntimeTests.configuration

    async def test_real_learning_result_can_be_saved_through_storage_worker(self):
        self.configuration()
        start = self.now - 1200
        keys = list(self.device["last_learning"]["entities"])
        rows = []
        for index in range(200):
            values = dict.fromkeys(keys, 5)
            if index < 100:
                values["g0_move" if index % 2 else "g2_still"] = 30
            rows.append(
                (
                    start + index * 6,
                    bytes(values.get(key, 255) for key in harness.constants.HISTORY_KEYS),
                )
            )
        harness.record_rows(self.runtime, "a", rows)
        self.device["history_labels"] = [
            {"start": start, "end": start + 600, "state": "present"},
            {"start": start + 600, "end": self.now, "state": "not_present"},
        ]
        learned = await self.runtime.async_learn("a")
        assert learned["status"] == "ok"
        await self.runtime.async_save()
        saved = self.runtime.store.async_save.call_args.args[0]
        assert saved["devices"]["a"]["last_learning"]["id"] == learned["id"]
        assert self.runtime._storage_status.get("error") is None
