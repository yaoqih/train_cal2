# stage4_simple

Fourth-stage residual closeout solver.

Scope:

- Starts from `stage3_simple` combined responses.
- Uses `physical.unsatisfied_cars` as the residual debt source.
- Does not handle residual depot / unwheel debt after stage 3; those cases are
  reported as partial.
- Counts each `Get`, `Put`, and `Weigh` row as one operation.
- Allows continuous carry across rows via `held`.
- Treats `联7` as blocked by the derailer in this phase.
- Reports `proved_within_move_model`, not unconditional global optimality.

Run:

```bash
python3 scripts/stage4_simple/solve.py data/truth2 \
  --stage3-out artifacts/stage3_simple_run \
  --out artifacts/stage4_simple_run \
  --verbose
```

Single case:

```bash
python3 scripts/stage4_simple/solve.py data/truth2 \
  --stage3-out artifacts/stage3_simple_run \
  --out artifacts/stage4_simple_probe \
  --case 0104W \
  --verbose
```
