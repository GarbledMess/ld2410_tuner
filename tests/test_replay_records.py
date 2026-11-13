"""A scoring pass decodes only the replay lists its scorers read.

Every pass decoded every interval list of every recorded run at every step, though a pass
scores a few patterns: in a repeat Learn that was 68,000 lists, two thirds never read.
"""

import random

import test_tuner  # noqa: F401
from tuner_under_test.engine.hybrid import replay


def _records(tmp_path, seed=1, steps=6):
    rng = random.Random(seed)
    rids = ["a", "b"]
    streams = [("a", (10,)), ("a", (20,)), ("b", (10,)), ("b", (30,))]
    records = replay.Records(tmp_path, rids, streams)

    def intervals():
        moment, found = 0.0, []
        for _ in range(rng.randint(0, 5)):
            moment += rng.uniform(0.5, 3)
            found.append((moment, moment + rng.uniform(0.1, 1)))
            moment = found[-1][1]
        return found

    shared = intervals()
    for step in range(steps):
        coverage = {"a": intervals(), "b": shared}
        outputs = [(intervals(), shared, intervals()) for _ in streams]
        records.write(step * 10.0, step * 10.0 + 10, coverage, outputs)
    return records, streams


def test_a_selective_read_decodes_the_same_lists_and_keeps_their_sharing(tmp_path):
    records, streams = _records(tmp_path)
    wanted = {streams[1], streams[2]}
    records.rewind()
    whole = [records.read() for _ in range(records.steps)]
    records.rewind()
    part = [records.read(wanted) for _ in range(records.steps)]
    for (lo, hi, coverage, outputs), (plo, phi, pcoverage, poutputs) in zip(
        whole, part, strict=True
    ):
        assert (lo, hi) == (plo, phi)
        assert {k: list(v) for k, v in coverage.items()} == {
            k: list(v) for k, v in pcoverage.items()
        }
        assert set(poutputs) == wanted
        for key in wanted:
            assert [list(x) for x in poutputs[key]] == [list(x) for x in outputs[key]]
        # One list stored once is one object wherever it is read, as in a whole read.
        a, b = poutputs[streams[1]][1], poutputs[streams[2]][1]
        assert a is b and a is pcoverage["b"]
    records.close()


def test_a_read_without_a_selection_still_decodes_every_stream(tmp_path):
    records, streams = _records(tmp_path, seed=2)
    records.rewind()
    assert set(records.read()[3]) == set(streams)
    records.close()


def test_an_overlap_holds_exactly_the_starts_ends_and_running_mass(tmp_path):
    from itertools import accumulate

    from tuner_under_test.engine.hybrid import scoring

    rng = random.Random(3)
    moment, intervals = 0.0, []
    for _ in range(500):
        moment += rng.uniform(0.001, 5)
        intervals.append((moment, moment + rng.uniform(0.001, 3)))
        moment = intervals[-1][1]
    for given in (intervals, [], intervals[:1]):
        found = scoring._Overlap(given)
        assert found.items is given
        assert found.starts == [a for a, _ in given]
        assert found.ends == [b for _, b in given]
        # Bit for bit: the same subtractions summed in the same order.
        assert found.mass == list(accumulate((b - a for a, b in given), initial=0.0))
