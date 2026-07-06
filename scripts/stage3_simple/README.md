# stage3_simple

Third-stage depot inbound solver built on the stage2 output state.

It tries the two fixed pickup templates:

- template B: `机走北 -> 机走棚 -> 洗油北 -> 机南`
- template A: `机走北 -> 机走棚 -> 机南`, then after the first depot-side clearing `洗油北`

The depot-side search is an operation-level Dijkstra over `Get(prefix)` and
`Put(suffix)` actions on repair-shop inner/outside lines. Process checks use
route reachability, pull limits, line length, and outside-line clearance for
inner repair-shop puts. Final acceptance checks depot slot rules with locked
south tails preserved.

Implementation notes:

- Stage 3 starts from a business re-couple at `存4线` north side after stage 2.
- Template-aware assignment is only a search guide. Final depot positions are
  judged from the actual inner-line order in the solved state, packed against
  the locked south tail.
- The first pass keeps the buffer set narrow: repair-shop outside lines only,
  with cars restricted to their assigned inner repair line.
- Partial cases are diagnostic rather than silent fallbacks. Common reasons are
  non-prefix active cars on an assembly line or route blockage from remaining
  occupied assembly/staging lines.
- Pure repair-shop-outside targets are reported as unsupported in this first
  pass instead of being silently ignored.

Run:

```bash
python3 scripts/stage3_simple/solve.py data/truth2 \
  --stage2-out artifacts/stage2_simple_final \
  --out artifacts/stage3_simple
```
