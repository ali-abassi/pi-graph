# Example certification — v0.1.1

The public examples were certified on 2026-07-19 using
`openai-codex/gpt-5.6-luna` at `medium` reasoning for every model-backed node,
judge, and QA call.

## Result

- 12 workflow contracts validated before execution.
- 13 live runs passed; the cache workflow ran twice.
- 11,828 tokens and $0.019479 provider-reported cost.
- 91.1 seconds total wall time.
- Sequential commands, parallel fan-out/fan-in, bounded retry, isolated LLM,
  typed JSON, deterministic routing, a read-only tool, a full agent effect,
  a judge repair loop, final QA, and cache replay all produced direct evidence.

The first certification pass found that QA usage appeared in `log.md` but was
missing from `ledger.json` and machine summaries. The runner was fixed to record
QA as the synthetic `__qa__` ledger node, a regression test was added, and the
entire certification was rerun before this result was accepted.

## Measured optimization lessons

| Signal | Evidence | Practical lesson |
|---|---:|---|
| Full agent | 5,964 tokens, 7.3s | Agent context is expensive; use it only when an open-ended tool loop is required. |
| Tool-limited node | 1,435 tokens, 8.4s | A least-privilege tool is cheaper than a full agent, but still costs more than supplying bounded context directly. |
| Isolated Luna nodes | 152–193 tokens in the simple cases | Prefer isolated prompts for bounded transformations. |
| Judge repair | 2,598 tokens, 36.8s, one retry | The judge improved the checklist from 2.5 to 8.0, but bounded semantic iteration was the largest latency and cost center. |
| Final QA | 641 tokens, 5.9s | QA is now visible in the same ledger as ordinary nodes. |
| Parallel analysis | 16.2s compute in 11.9s wall time | Two independent model nodes overlapped before deterministic fan-in and QA. |
| Cache replay | 257 tokens and 6.1s, then 0 tokens and 0.3s | Identical passing model work can be eliminated while the gate still reruns. |
| Retry recovery | one failed attempt, then pass | Transient failure remains explicit in `log.md` and consumes only the declared retry budget. |

These numbers describe this fixture and runtime, not universal model pricing or
latency. The public suite deliberately uses the same low-cost model for final QA;
that proves isolated review behavior, not independent model diversity or judge
calibration.

## Reproduce

```bash
npm run test:examples
python3 scripts/run_example_suite.py
```

The live command writes a gitignored `examples/.artifacts/<run>/report.md` plus
every underlying `log.md`, `ledger.json`, artifact, rejected judge candidate,
QA report, and per-run git history. See [`examples/README.md`](../examples/README.md)
for individual commands and the graduated catalog.
