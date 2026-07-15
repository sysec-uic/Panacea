# Cross-project transfer experiment

Explores whether a heuristic learned on one ARVO project transfers to bugs in a
different project. Not wired into the active control/treatment mruby pipeline
(`learn_loop.py`) — a separate, currently-paused track.

Design: `../../docs/superpowers/specs/2026-06-30-cross-project-transfer-experiment-design.md`

Imports top-level modules (`build_instance`, `injector`, `ledger`,
`playbook_store`), so run from `arvo-eval/` with it on `PYTHONPATH`:

```bash
cd arvo-eval
PYTHONPATH=. python3 transfer/transfer_runner.py
```
