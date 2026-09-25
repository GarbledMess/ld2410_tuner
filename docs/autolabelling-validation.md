# Automatic labelling validation

Version 1.9.3 uses `temporal_evidence_v2`. The changes concern automatic training
labels only. Threshold learning and application still require explicit commands.

## Method

The supplied store contains 183,720 recorded samples across five devices with
history; ten device records were checked for cleanup parity. A newer label export
was overlaid for one device. Reports use anonymous device indices and do not retain
names, identifiers, room names, absolute timestamps, or the input data.

`tools/replay_autolabels.py` replays recordings chronologically against a saved v1
estimator and the current estimator. It evaluates three scenarios per device:

- **Bootstrap:** no human reference distributions.
- **Causal:** earlier development labels become references after each prediction.
- **First half:** only earlier development labels in the first half of the
  recording become references.

The latest 20% of each labelled class is evaluation-only in every scenario. Those
labels never train the replayed estimator. Unlabelled observations may update its
background, as in live use. The v1 adapter retains its original callback-count
confirmation; v2 uses its actual elapsed-time confirmation function.

## Withheld sample results

Device A has 574 withheld presence and 1,976 withheld empty-room observations.
Device B has 1,483 withheld presence and 334 withheld empty-room observations.

| Device / scenario | Presence detected, before → after | False presence in empty room, before → after | Empty room correctly labelled, before → after |
| --- | ---: | ---: | ---: |
| A / bootstrap | 574 → 573 | 24 → 0 | 1,354 → 1,932 |
| A / causal | 574 → 573 | 28 → 29 | 1,320 → 1,689 |
| A / first half | 574 → 573 | 20 → 13 | 1,329 → 1,765 |
| B / bootstrap | 0 → 0 | 0 → 0 | 324 → 322 |
| B / causal | 0 → 1,286 | 1 → 0 | 180 → 334 |
| B / first half | 0 → 1,286 | 334 → 0 | 0 → 334 |

Remaining observations in these rows are UNKNOWN, not correct classifications.
Device A gains substantially more usable empty-room evidence without losing any
additional presence detections over its full labelled recording: 2,865 of 2,868
presence samples are detected before and after. However, one withheld presence
sample now becomes UNKNOWN, and the causal scenario has one extra withheld false
positive. The result is not a uniform improvement in every slice.

Device B's incomplete channels previously blocked all presence guesses. Strong
human-guided evidence now supports useful presence labels on available channels,
with confidence capped at 60%. Its remaining 197 withheld presence samples stay
UNKNOWN. Bootstrap alone still detects none of its recorded presence.

The other three recorded devices have only empty-room labels and partial channels.
Their 560, 671 and 672 withheld observations remain UNKNOWN before and after. They
cannot establish presence performance and should not be counted as successes.

## What the evidence establishes

Typical empty-room energy is less likely to accumulate false positive support.
Sustained human-guided signals can produce useful guesses with missing channels.
Elapsed-time integration avoids treating faster callbacks as independent votes.
Flat startup, missing channels and unsupported filter memory remain uncertain.

Confidence is still heuristic, with caps of 65% for bootstrap, 80% for partly
human-guided and 98% for fully guided observations. These caps are not measured
accuracy. The partial-channel cap is 60%. No result here proves near-perfect
real-world detection.

The recording interval is normally six seconds while live estimation runs every
two seconds; intermediate live readings cannot be reconstructed. Adjacent samples
are correlated, the class-wise holdout is from the same recordings, and causal
replay assumes labels were available at that time although some were retrospective.
Some scenario improvements may depend on those references. Independent room
sessions, quiet sitting and empty-room trials are still needed. False bursts,
longest missed runs and confidence buckets are included in the machine-readable
replay output, rather than reducing evaluation to one accuracy percentage.

## Reproduce privately

Keep recordings and saved baseline code outside the repository:

```sh
python3 tools/replay_autolabels.py /private/store.data \
  --baseline /private/inference-v1.py \
  --labels /private/newer-labels.json \
  --output /private/replay-report.json
```

The tool never modifies its inputs. CI tests use synthetic recordings, assert that
holdout labels cannot enter references, and verify the metric and anonymization
contracts. Refactoring the replay produced exactly the same 15 scenario reports.
