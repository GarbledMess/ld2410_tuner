# Adaptive reference validation — 1.16.0

The change lets continuing human and entity labels correct the radar estimator,
retains bounded learned summaries after raw-history expiry, and reduces old
references' influence. It changes automatic labels, not the LD2410 presence output
or the existing explicit Learn/Apply workflow.

## Deterministic changing-room checks

`tests/test_references.py` exercises production reference/inference code and the
runtime recording/correction paths:

- An early human baseline has empty energy 8 and quiet presence 70. Thirty days
  later, a new empty-room energy 25 is initially inferred as present. A confirmed
  entity absence period teaches the new background; 25 then becomes Not Present
  while 70 remains Present.
- After forty days, entity-corrected background energy 50 and quiet presence 70
  generate automatic-only fitting evidence. The resulting threshold is at least
  50 and below 70, with no false-positive or missed-presence samples in that
  synthetic fitting set. It does not claim held-out or hardware accuracy.
- Forty successive days of quiet presence do not add that signal to the negative
  reference distribution. Repeated inferred labels do not raise the independent
  confidence cap; inferred-only history cannot establish independent references.
- Same-recording human labels override disagreeing entity labels. Retrospective
  corrections rebuild human references and remove overlapping entity/inferred
  summaries. Explicit Unknown excludes records from profile learning.
- Positive source buffers exclude their boundaries before teaching the model.
  Entity confidence, source reconfiguration, retention, restart, Clear data and
  legacy histogram import are exercised. Batched imports match live updates.

These scenarios prove the specified behavior under controlled inputs. They do
not demonstrate reliable unsupervised separation of every fan, pet and person.

## Private chronological replay

The supplied recording spans about 5.51 days. Only one recorded device has both
human-labelled states in both chronological halves. Its first 2.76 days supplied
human references after each prediction; later human labels were evaluation-only.
The adaptive run used the production runtime classifier and confirmation filter,
with eligible inferred observations updating the weaker radar reference pool.
No later human labels were used for adaptation.

| Later labelled readings | Existing human-guided estimator | Adaptive references |
| --- | ---: | ---: |
| Present correctly labelled Present | 10,753 | 10,753 |
| Present incorrectly labelled Not Present | 0 | 0 |
| Present labelled Unknown | 0 | 0 |
| Not Present correctly labelled Not Present | 2,244 | 2,246 |
| Not Present incorrectly labelled Present | 36 | 39 |
| Not Present labelled Unknown | 92 | 87 |

This is not a uniform improvement: there are three additional false-positive
labels and five fewer unknown labels. The new ongoing-correction behavior is
proven by the changing-background tests; the replay does not establish better
overall accuracy or months of drift resistance. The numbers are autolabelling
sample outcomes, not device output after timeout/delays or measured error durations.

Both replays treat supplied human labels as the reference, including any mistakes
or retrospective edits. Sparse recordings cannot reconstruct intervening live
samples. Neither the confidence cap nor agreement with the estimator's own
labels is a measured real-world accuracy guarantee.

## Persistence and cost

The replay's final profile was 77,772 bytes with ordinary JSON serialization,
containing 28 human and 48 inferred periods. Its compressed distributions count
towards the existing total-storage budget; there is no second raw recording store.
The profile is bounded at 24 periods per source and state.

A batched rebuild using the supplied device's retained human recordings took
approximately 0.81 seconds on the development environment. Startup imports run
in Home Assistant's executor. Historical correction rebuilds remain synchronous,
as does the existing histogram rebuild; this measurement is not a guarantee for
larger histories or slower Home Assistant hosts.

Local verification: 395 Python tests passed with 97.09% aggregate coverage;
browser checks passed including the new in-card reference and unfamiliar-reading
messages. Python lint, the cognitive-complexity limit of 10 and JavaScript format
checks passed. These checks do not replace a deployed Home Assistant trial.
