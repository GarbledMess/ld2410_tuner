"""Boundary refusals remain explicit before accepting partial numerical results."""

from concurrent.futures import ThreadPoolExecutor

import pytest
import test_tuner  # noqa: F401
from test_event_fitting import view
from test_event_quality import block
from tuner_under_test.history import label_counts, label_lookup
from tuner_under_test.worker import bridge, calculations, capture, codec, display, history, protocol


def test_open_live_label_has_the_same_reader_after_finite_wire_conversion():
    original = label_lookup.label_index({"training_label_start": 10, "training_state": "present"})
    finite = codec.decode(codec.encode(label_counts._finite_index(original)))
    for timestamp in (0, 10, 20, 1e20):
        assert label_lookup.index_reader(finite)(timestamp) == label_lookup.index_reader(original)(
            timestamp
        )


def test_metadata_allows_large_installations_but_still_has_a_count_bound():
    from types import SimpleNamespace

    owner = SimpleNamespace(data={"devices": {str(i): {} for i in range(50)}})
    bridge.preflight(owner)
    owner.data["devices"] = {str(i): {} for i in range(129)}
    with pytest.raises(ValueError, match="device/group budget"):
        bridge.preflight(owner)


@pytest.mark.parametrize("budget", ["max_rows", "max_raw_bytes", "max_file_bytes"])
def test_child_rejects_aggregate_budget_before_returning_day(tmp_path, budget):
    source = view([block(0, 600, 40)], [])
    with ThreadPoolExecutor() as executor:
        request = executor.submit(capture.prepare, source, "any-name", tmp_path).result()
    request["budgets"][budget] = 0
    restored = calculations.restore(request)
    with pytest.raises(history.HistoryRejected, match="budget|oversized"):
        restored._days.day("any-name", restored._days.days("any-name")[0])


def test_restore_rejects_unrecognized_view_attributes():
    with pytest.raises(ValueError, match="view attribute"):
        calculations.restore({"data": {}, "days": [], "budgets": {}, "attributes": {"hass": 1}})


def test_missing_selected_day_returns_no_data():
    from datetime import date

    days = calculations.ReadOnlyDays({"days": [], "budgets": {}})
    assert days.day("missing", date(1970, 1, 1)) is None


def test_result_rejects_extra_envelope_fields():
    with pytest.raises(ValueError, match="result payload"):
        codec.unpack_result(protocol.pack("result", {"value": 1, "extra": 2}))


def test_display_rejects_unrecognized_operation():
    with pytest.raises(ValueError, match="display operation"):
        display.calculate("untrusted", None, {})
