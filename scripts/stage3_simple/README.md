# stage3_simple

Stage 3 depot-inbound solver over the replayed Stage 2 state.

## Solver structure

The solver has three explicit layers:

1. `Terminal layout` assigns every inbound car to a legal depot line and slot.
   It compares a run-cohesive dynamic program with exact minimum-cost slot
   matching. Locked south-tail cars remain fixed.
2. `Alignment planner` operates on slot-aware state. Canonical block actions
   put a ready deep-slot suffix, stage the tail hiding the next slot, or
   retrieve the shortest useful buffer prefix. One-door clear, two-door swap,
   and forced-outer parking are bounded atomic transactions: a transaction is
   committed only after its final state and original target block are proved.
3. `Physical execution` validates every Get/Put through route reachability,
   access-end order, pull limits, track length, depot-door clearance, and
   explicit `Positions` semantics.

There is no failure-driven or silent fallback. Up to 32 greedy candidates are
declared and evaluated independently across these dimensions:

- pickup template `A` / `B`;
- terminal layout `cohesive` / `cost`;
- deferred blocker clear `on` / `off` when applicable;
- compatible terminal-block merge `on` / `off`;
- inner-door clear policy `eager` / `just_in_time`.

Each candidate keeps its own status and rejection reason in
`template_summaries`. Replay, combined replay, terminal rules, and the internal
State/replay projection are validated before candidates enter final selection.
Exact operation search is a separately labelled
`inner_clear_policy=exact` strategy and is only declared for cases with at
most six active cars. For larger cases, the block planner returns a bounded
diagnostic partial instead of entering an unbounded operation search. A small
proof-directed exact search may also be declared to improve a complete
incumbent whose admissible gap is at most two hooks.

## Contracts and diagnostics

- Input car length, repair process, locomotive endpoint, and all four depot
  capacity modes are mandatory. Missing capacity data is rejected; no
  capacity or car-length default is used.
- Stage 2 combined replay must be schema/physical/business/state clean before
  solving starts.
- Final `complete` requires Stage 3 replay, combined replay, depot slot rules,
  locked-stayer rules, and the terminal business boundary to agree.
- Partial responses expose neither operations nor generated end state. The
  attempted trajectory and residual business violations remain separate
  diagnostics.
- `task_nos` owns Stage 3 work, `restoration_nos` owns physically displaced
  context, and `stage3_business_nos` owns final depot business. A support car
  does not acquire Stage 3 weighing debt merely because a door clear touches it.
- Depot state stores real sparse slot positions. Single-position Force targets
  are hard dependencies; multi-position targets remain flexible legal sets.
- Movable cars occupying the wrong depot outer line are included in the active
  closure instead of being treated as fixed door blockers.
- Assignment failures include capacity certificates when infeasibility is
  proved, such as `inner_slot_capacity_infeasible` and
  `outer_capacity_infeasible`.
- Cohesive-layout rejection distinguishes direct-unload order conflicts from
  global slot infeasibility. Batch execution is fail-fast on programming errors
  and does not convert exceptions into synthetic solver results.

The fixed-strategy admissible hook bound is reported as:

```text
LB = source_gets + inner_puts + non_inner_puts + frontier_rehandle
```

Negative gaps are never clamped. Any observed `LB > hooks` is exposed as
`invalid_lower_bound_certificate` and listed in
`lower_bound_validation_violations`.

Per-case output also includes `<case>_assignment_plan.json`, with source,
assigned line/slot, exposure rank, allowed targets, and active constraints.

## Run

Truth2 current pipeline:

```bash
python3 scripts/stage3_simple/solve.py data/truth2 \
  --stage2-out artifacts/four_stage_balanced_current/stage2 \
  --out artifacts/stage3_no_fallback_final_20260711_v4/truth2
```

Truth3 current pipeline:

```bash
python3 scripts/stage3_simple/solve.py data/truth3 \
  --stage2-out artifacts/stage1-4_simple/truth3/stage2 \
  --out artifacts/stage3_no_fallback_final_20260711_v4/truth3
```

Latest full probes:

```text
truth2: 113 cases, 111 complete, 2 upstream partial
        922 hooks, avg 8.306, max 14
        wall 36.94 s, max RSS 55.8 MiB, replay hard violations 0
        0104Z=9, 0112Z=10, 0117Z=14, 0226W=10

truth3: 34 cases, 30 complete, 2 upstream partial, 2 proved infeasible
        463 hooks, conditional completion 30/32, avg 15.433, max 33
        wall 6.25 s, max RSS 37.6 MiB, replay hard violations 0
        0408W=17, 0416W=33, 0420W=28, 0427W=27, 0429Z=16
        0406W: 14 cars > 13 reachable depot slots
        0424Z: 52.8 m > 49.3 m on the only allowed outer line
```

Against the prior full probes this keeps the same completion boundary while
reducing Stage 3 by 5 hooks on truth2 and 7 hooks on truth3. On complete cases,
the selected fixed-strategy relaxation totals `873/922` hooks on truth2 and
`259/463` on truth3, leaving certified gaps of 49 and 204 hooks. The weaker
assignment-independent portfolio bounds are 782 and 217. These are relaxation
certificates, not proofs of the global optimum or exact distances to it.

Latest Stage 4 linkage over these frozen Stage 3 artifacts:

```text
truth2: Stage4 operational complete 100/113, conditional 100/111
        strict end-to-end business-clean 98/113
truth3: Stage4 operational complete 23/34, conditional 23/30
        strict end-to-end business-clean 22/34
```

All operational-complete responses have clean physical and state replay. The
strict count additionally rejects `0114Z`, `0129W`, and `0421W` for final line
length violations already present, unchanged, in the original requests. They
are not introduced by Stage 3. The strict complete chains total 3363 hooks over
98 truth2 cases and 609 hooks over 22 truth3 cases for Stage 3 plus Stage 4.
Stage 4 was executed from the `v3` directories; every Stage 4 input file
(`stage3_request`, `response`, and `combined_response`) is byte-identical in
`v3` and the final `v4` freeze.
