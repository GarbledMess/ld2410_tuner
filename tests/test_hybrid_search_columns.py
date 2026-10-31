"""Columnar conditional decisions for zones beyond one packed block."""

import random

import pytest
import test_tuner  # noqa: F401
from test_hybrid_evidence import truth
from test_hybrid_search import problem
from tuner_under_test.engine.hybrid import search_columns, search_masks
from tuner_under_test.engine.hybrid.decision import Decision
from tuner_under_test.engine.hybrid.search import collect_training
from tuner_under_test.engine.hybrid.search_scores import conditional_costs
from tuner_under_test.engine.hybrid.training import TrainingStore


class Support:
    def supports(self, key, value):
        return value % 4 != 1


def large(seed):
    rng = random.Random(seed)
    vectors = {
        rid: [tuple(rng.choice((0, 9, 20, 21, 40, 70, 100)) for _ in range(3)) for _ in range(90)]
        for rid in ("a", "b")
    }
    spans = [
        truth(
            i * 54,
            (i + 1) * 54,
            "present" if i % 2 else "not_present",
            source=rng.choice(["human", "entity"]),
            confidence=rng.choice([1, 0.6, 0.75]),
            episode=str(i // 3),
        )
        for i in range(10)
    ]
    p = problem(vectors, spans)
    if seed % 2:
        from dataclasses import replace

        p = replace(
            p, policy=replace(p.policy, automatic_weight=0.5, quality={"models": {"a": Support()}})
        )
    return p


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("attenuate", [False, True])
def test_columnar_conditional_lanes_equal_streaming_decisions(
    tmp_path, monkeypatch, seed, attenuate
):
    monkeypatch.setattr(search_masks, "packed_block", lambda *args: None)
    p = large(seed)
    rng = random.Random(seed)
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        for _ in range(4):
            pattern = {
                rid: {c: rng.choice((0, 9, 20, 40, 100)) for c in range(3)} for rid in ("a", "b")
            }
            rid, channel = rng.choice(["a", "b"]), rng.randrange(3)
            args = (p, store, pattern, rid, channel, tuple(range(101)))
            store.__dict__.pop("_column_blocks", None)
            monkeypatch.setattr(search_columns, "COLUMN_BYTES", 0)
            streaming = conditional_costs(*args, attenuate=attenuate)
            store.__dict__.pop("_column_blocks", None)
            monkeypatch.setattr(search_columns, "COLUMN_BYTES", 32 * 1024**2)
            columnar = conditional_costs(*args, attenuate=attenuate)
            assert search_columns.column_block(p, store, p.zones[0], attenuate) is not None
            assert columnar == pytest.approx(streaming, abs=1e-9)


def test_large_zone_conditional_costs_never_visit_vector_crossings(tmp_path, monkeypatch):
    monkeypatch.setattr(search_masks, "packed_block", lambda *args: None)
    p = large(1)

    def forbidden(*args):
        raise AssertionError("per-vector crossing in a columnar zone")

    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        monkeypatch.setattr(Decision, "crossing", forbidden)
        conditional_costs(p, store, {r: dict.fromkeys(range(3), 40) for r in "ab"}, "a", 0, (0, 40))


def test_column_block_respects_its_byte_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(search_masks, "packed_block", lambda *args: None)
    monkeypatch.setattr(search_columns, "COLUMN_BYTES", 10)
    p = large(2)
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        assert search_columns.column_block(p, store, p.zones[0], False) is None


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("attenuate", [False, True])
def test_columnar_episode_costs_equal_unfolded_cells(tmp_path, monkeypatch, seed, attenuate):
    from tuner_under_test.engine.hybrid.search_scores import coordinate_costs

    monkeypatch.setattr(search_masks, "packed_block", lambda *args: None)
    p = large(seed)
    rng = random.Random(seed + 100)
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        for _ in range(4):
            pattern = {rid: {c: rng.choice((0, 9, 20, 40, 100)) for c in range(3)} for rid in "ab"}
            rid, channel = rng.choice("ab"), rng.randrange(3)
            args = (p, store, pattern, rid, channel, (0, 9, 20, 21, 40, 70, 100))
            unfolded = coordinate_costs(*args, folded=False, attenuate=attenuate)
            columnar = coordinate_costs(*args, attenuate=attenuate)
            assert search_columns.column_block(p, store, p.zones[0], attenuate) is not None
            assert columnar == pytest.approx(unfolded, abs=1e-8)


def test_large_zone_coordinate_costs_never_visit_vector_crossings(tmp_path, monkeypatch):
    from tuner_under_test.engine.hybrid.search_scores import coordinate_costs

    monkeypatch.setattr(search_masks, "packed_block", lambda *args: None)
    p = large(3)

    def forbidden(*args):
        raise AssertionError("per-vector crossing in a columnar zone")

    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        monkeypatch.setattr(Decision, "crossing", forbidden)
        coordinate_costs(p, store, {r: dict.fromkeys(range(3), 40) for r in "ab"}, "b", 1, (9, 40))
