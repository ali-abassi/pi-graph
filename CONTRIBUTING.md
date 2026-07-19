# Contributing

Pi Workflows welcomes focused fixes, examples, documentation, and workflow
runtime improvements.

## Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'PyYAML>=6,<7' 'ruamel.yaml>=0.18,<0.19'
npm ci --ignore-scripts
npm test
npm run check
npm run test:examples
```

## Expectations

- Keep `steps.yaml` as the execution source of truth and update
  `schemas/workflow.schema.json` when its public contract changes.
- Use deterministic code for routing, schemas, gates, permissions, budgets,
  and completion. Models may generate or review artifacts.
- Add the smallest regression check for a behavior change.
- Never commit credentials, provider auth, generated runs, caches, customer
  data, or `examples/.artifacts/` evidence.
- Keep ordinary workflows on the lean runner. The production factory is an
  explicit opt-in for versioned certification and replay guarantees.

## Pull requests

Explain the user-visible behavior, failure mode, and verification. A change is
ready when the focused checks pass, `git diff --check` is clean, and public docs
match the actual command and output contracts.
