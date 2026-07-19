# Public quality audit contract — 2026-07-19

- **Repository:** the canonical Pi Workflows checkout and public GitHub remote.
- **Mode:** repair, live certification, and re-audit.
- **Definition of done:** at least ten graduated, reusable workflows validate
  mechanically and pass live; all model-backed example calls use Luna at medium
  reasoning; logs, ledgers, retries, cache, parallelism, tools, agent effects,
  judges, and QA are inspectable; the checkout is clean, documented, remotely
  installable, and green in public CI.
- **Included:** core runner, CLI, schemas, Pi extension, installer, examples,
  live example harness, tests, factory, documentation, package metadata, and CI.
- **Excluded:** generated `.artifacts/`, workflow `runs/`, caches, virtual
  environments, dependencies, provider infrastructure, and Loops/Agent X source
  repositories. Integrations are verified at their declared package boundary.
- **Protected behavior:** DAG ordering, concurrency, typed routes, gates,
  retries, judges, cache, QA, immutable inputs, run evidence, cost accounting,
  and standalone installation.
- **Required checks:** complete local suite, TypeScript, Python compile, example
  validation, 12-workflow Luna-medium live certification, secret/dependency
  scan, clean install from the public remote, and macOS/Linux CI.
- **Repair authority:** repository cleanup, focused fixes, examples,
  documentation, commits, public pushes, and a patch release are authorized by
  the request. Generated live evidence remains local and gitignored.
- **Non-goals:** npm publication, redesigning Agent X or Loops, claiming model
  determinism, or calling same-model QA calibrated independent evidence.
