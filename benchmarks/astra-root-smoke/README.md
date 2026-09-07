# Astra root-model smoke observations

Six contributor-run cases compared Sol high, Sol xhigh and Astra low through
Remora on one small bug. Astra low finished the direct case faster, at a higher
Standard API-equivalent cost. It was slower in this delegated sample. The
Astra/Sol/Luna entry choices expose that trade-off; these observations do not
establish a universal best configuration.

## Setup and limits

| Property | Recorded condition |
| --- | --- |
| Source | Remora base e4b0439 with the plugin-isolation and policy-alignment candidate |
| Client | Calico-patched Claude Code 2.1.263 |
| Task | Repair stale committed usage while preserving immutable message snapshots |
| Direct cases | Root works without a child |
| Delegated cases | Exactly one background executor, configured as Luna max, collected before completion |
| Order | D-C, D-B, E-C, E-B, D-A, E-A |
| Deadline | 360 seconds per case; no retry was needed |
| Sample | One completed observation per configuration and shape |

The [fixture](./fixture) comes from
[Pilotfish commit 863b117](https://github.com/Nanako0129/pilotfish/tree/863b117b9da42179c5bb77a05158920fbc092ee2/benchmarks/dispatch-brake/fixture)
and retains its MIT license. [Direct](./direct.txt) and
[delegated](./delegated.txt) prompts are included. Personal user-level
instructions and unrelated plugins remained active. Raw sessions, account
identifiers and private configuration are not published. The public files are
not a snapshot of the whole original environment.

## Results

| Shape | Root | Seconds | Standard API-equivalent USD |
| --- | --- | ---: | ---: |
| Direct | Sol xhigh | 133.764 | 0.51687760 |
| Direct | Sol high | 123.875 | 0.47340240 |
| Direct | Astra low | 92.714 | 0.94611400 |
| Delegated | Sol xhigh | 159.325 | 0.29226728 |
| Delegated | Sol high | 149.263 | 0.30887064 |
| Delegated | Astra low | 218.130 | 0.70264952 |

All six cases passed both original tests and the external
[acceptance check](./acceptance.mjs). Each delegated case used one bare
`executor` without an invocation-level model override, followed by completion
and blocking result collection. Child messages reported Luna; provider-side
execution of the configured effort was not measured.

Every live init omitted the canonical Pilotfish plugin and its namespaced
roles while retaining Remora's bare roles. Separate localhost fake-provider
checks observed the Pilotfish SessionStart text before the fix and its absence
afterward, without a paid provider request. The Sonnet alias reached Sol.
These checks cover the canonical installed plugin, not managed force-enables,
alternate plugin IDs or explicitly loaded custom plugin directories.

## Accounting

[results.json](./results.json) contains aggregate token categories, fixture and
artifact hashes, tested runtime-file hashes and the dated
[OpenAI Standard rates](https://developers.openai.com/api/docs/pricing).
Anthropic-shaped `modelUsage.inputTokens` is uncached input. Cache reads and
creation are separate; unlike Codex's input total, they must not be subtracted
from it. Per-model `modelUsage` includes child models and is summed once.
Root-only `result.usage` must not be added again.

```text
API equivalent = sum over models of
  (uncached input × input rate + cached input × cache rate
   + cache creation × write rate + output × output rate) / 1,000,000
```

The client reported Standard. Its dollar display had `costBasis=unknown`, so
these costs were recomputed from tokens rather than copied from that display.
A zero reported thinking-token field does not prove zero provider reasoning;
reported output is counted once. These estimates are not subscription bills.

Subscription quota attribution is inconclusive: integer account-level readings
also include controller activity and possible delayed accounting. The
[official rate card](https://help.openai.com/en/articles/11481834-chatgpt-rate-card-business-enterpriseedu/)
distinguishes purchased-credit prices from included limits. No universal
subscription multiplier, general quality ranking or causal speed claim is
inferred from this fixed-order sample.

## Offline checks

From the repository root, verify the published arithmetic without a model call:

```bash
python3 benchmarks/astra-root-smoke/verify_costs.py
```

The included fixture deliberately contains the bug. After a separately
approved implementation run in a disposable copy, check that completed copy:

```bash
node benchmarks/astra-root-smoke/acceptance.mjs ./completed-fixture
npm --prefix ./completed-fixture test
```

The check covers committed and provisional usage, prior-state immutability,
canonical-message aliasing, unknown IDs and unrelated events. The public files
reproduce arithmetic and acceptance checks, not the original private context
or exact timing.
