# Legacy

The original single-bug runner, from before `learn_loop.py` automated the
control/treatment passes. Superseded — see the top-level `arvo-eval/README.md`
for the current pipeline.

Kept for reference. These import top-level modules (`build_instance`,
`playbook_store`, ...), so run them from `arvo-eval/` with it on `PYTHONPATH`:

```bash
cd arvo-eval
PYTHONPATH=. python3 legacy/run_single.py
```
