from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import replay_validator as rv  # noqa: E402
from stage3_simple import placement, transactions  # noqa: E402
from stage3_simple.solve import (  # noqa: E402
    Op,
    PlacementSpec,
    SearchResult,
    Stage3Solver,
    State,
    diagnostic_summary,
)


EMPTY_STAGE2 = {"Data": {"Operations": []}}


def request(cars: list[dict[str, object]]) -> dict[str, object]:
    return {
        "StartStatus": cars,
        "TerminalLines": [
            {"Line": f"修{index}库内", "IsInspectionMode": False}
            for index in range(1, 5)
        ],
        "locoNode": {"Line": "存4线", "End": "North"},
    }


def car(
    no: str,
    line: str,
    position: int,
    targets: list[str],
    *,
    length: float = 14.3,
    process: str = "段修",
    forced: list[int] | None = None,
    weigh: bool = False,
    weighed: bool = False,
) -> dict[str, object]:
    return {
        "No": no,
        "Line": line,
        "Position": position,
        "Length": length,
        "RepairProcess": process,
        "TargetLines": targets,
        "ForceTargetPosition": forced or [],
        "IsWeigh": weigh,
        "_Weighed": weighed,
    }


class Stage3StructuralTests(unittest.TestCase):
    def prepare_unified_assignment(
        self,
        solver: Stage3Solver,
        template: str = "B",
    ) -> str:
        solved = solver.unified_placements(template)
        self.assertTrue(solved.complete)
        self.assertTrue(solved.plans)
        layout = f"unified:{template}:00"
        solver.assigned_line_by_no = solver.build_assigned_line_by_no(template, layout)
        return layout

    def assert_replay_clean(
        self,
        req: dict[str, object],
        result: dict[str, object],
    ) -> None:
        self.assertEqual(rv.replay(result["stage3_request"], result["response"])[1], [])
        self.assertEqual(rv.replay(req, result["combined_response"])[1], [])

    def test_legacy_fallback_methods_are_absent(self) -> None:
        legacy_methods = (
            "build_cost_assigned_line_by_no",
            "build_cohesive_assigned_line_by_no",
            "exact_operation_lower_bound_components",
            "greedy_finish",
            "greedy_get_blocking_inner",
            "greedy_put_ready_inner",
            "greedy_put_buffer_block",
            "greedy_clear_gate_macro",
            "greedy_nested_gate_swap",
            "try_apply_deferred_clear_macro",
            "search",
            "priority",
            "reconstruct",
        )

        for name in legacy_methods:
            with self.subTest(name=name):
                self.assertFalse(hasattr(Stage3Solver, name), name)
        self.assertFalse(hasattr(transactions, "enumerate_minimal_transactions"))

    def test_global_placement_lower_bound_violation_is_reported(self) -> None:
        req = request([car("A", "机走北", 1, ["修1库内"])])
        original_solve = placement.solve

        def invalid_placement_bound(*args: object, **kwargs: object) -> placement.SolveResult:
            solved = original_solve(*args, **kwargs)  # type: ignore[arg-type]
            self.assertIsNotNone(solved.lower_bound)
            return replace(
                solved,
                lower_bound=(99, *solved.lower_bound[1:]),  # type: ignore[index]
            )

        with patch.object(placement, "solve", side_effect=invalid_placement_bound):
            result = Stage3Solver(
                "TEST",
                req,
                EMPTY_STAGE2,
                time_budget_seconds=5,
            ).solve()

        self.assertEqual(result["summary"]["status"], "complete")
        self.assertEqual(
            result["summary"]["optimality_status"],
            "invalid_lower_bound_certificate",
        )
        self.assertIn(
            {
                "operation_lower_bound_scope": "all_placement_relaxation",
                "operations": result["summary"]["business_hooks"],
                "operation_lower_bound": 100,
            },
            result["summary"]["lower_bound_validation_violations"],
        )

    def test_invalid_stage2_combined_response_is_explicitly_rejected(self) -> None:
        req = request([car("A", "机走北", 1, ["修1库内"])])
        invalid_stage2 = {
            "Data": {
                "Operations": [
                    {
                        "Index": 1,
                        "Action": "Get",
                        "Line": "机走北",
                        "MoveCars": ["UNKNOWN"],
                        "TrainCars": ["UNKNOWN"],
                        "PassbyPath": ["机走北"],
                    }
                ]
            }
        }

        with self.assertRaisesRegex(ValueError, "stage2.*replay"):
            Stage3Solver("TEST", req, invalid_stage2, time_budget_seconds=5)

    def test_terminal_capacity_mode_has_no_implicit_default(self) -> None:
        req = request([car("A", "机走北", 1, ["修1库内"])])
        del req["TerminalLines"][0]["IsInspectionMode"]

        with self.assertRaisesRegex(
            ValueError,
            "terminal_inspection_mode_missing_or_invalid",
        ):
            Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)

    def test_deferred_staging_lines_share_one_terminal_equivalence_class(self) -> None:
        req = request([car("D", "机走北", 1, ["油漆线"])])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        solver.active_nos = {"D"}
        solver.assigned_line_by_no = {"D": "修1库外"}
        solver.assigned_slot_by_no = {}
        state = State(
            lines=(("卸轮线", ("D",)),),
            held=(),
            loco=("修1库外",),
            phase=1,
        )

        self.assertTrue(solver.assignment_line_satisfied("D", "卸轮线"))
        self.assertTrue(solver.assignment_line_satisfied("D", "修4库外"))
        self.assertFalse(solver.assignment_line_satisfied("D", "机走北"))
        self.assertEqual(solver.stable_closure(state, frozenset()), frozenset({"D"}))
        self.assertEqual(solver.transaction_progress_key(state, frozenset({"D"}))[0], 0)
        self.assertTrue(solver.complete(state))

    def test_transaction_expansion_limit_must_be_positive(self) -> None:
        req = request([car("A", "机走北", 1, ["修1库内"])])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        layout = self.prepare_unified_assignment(solver)

        with self.assertRaisesRegex(
            ValueError,
            "transaction_expansion_limit_must_be_positive",
        ):
            solver.solve_template(
                "B",
                layout=layout,
                transaction_expansion_limit=0,
            )

    def test_layout_search_releases_prior_state_materializations(self) -> None:
        req = request([car("A", "机走北", 1, ["修1库内"])])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        layout = self.prepare_unified_assignment(solver)
        stale = State(lines=(), held=("STALE",), loco=("联7",), phase=1)
        solver.cars_cache[stale] = ()
        solver.line_map_cache[stale] = {}

        result = solver.solve_template("B", layout=layout)

        self.assertEqual(result.status, "complete")
        self.assertNotIn(stale, solver.cars_cache)
        self.assertNotIn(stale, solver.line_map_cache)

    def test_complete_summary_requires_combined_replay_to_be_clean(self) -> None:
        req = request([car("F", "存4线", 1, ["存4线"])])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        state = solver.initial_state_without_pickup()
        chosen = SearchResult(
            status="complete",
            template="none",
            state=state,
            ops=(),
            cost=(0, 0, 0, 0),
            reasons=(),
            expansions=0,
            elapsed_seconds=0.0,
        )
        replayed = [dict(item) for item in solver.initial_cars]
        combined_violation = rv.V(
            1,
            "physical",
            "combined_replay_test_violation",
            "combined response is invalid",
        )

        with patch.object(
            rv,
            "replay",
            side_effect=[
                (replayed, []),
                (replayed, []),
                (replayed, [combined_violation]),
            ],
        ):
            result = solver.result(chosen, [chosen])

        self.assertFalse(result["summary"]["combined_replay_physical_ok"])
        self.assertEqual(result["summary"]["status"], "partial")
        self.assertTrue(
            any(
                "combined_replay_test_violation" in reason
                for reason in result["summary"]["blocking_reasons"]
            )
        )

    def test_state_replay_mismatch_is_rejected_before_candidate_choice(self) -> None:
        req = request([car("A", "机走北", 1, ["修1库内"])])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        layout = self.prepare_unified_assignment(solver)
        valid = solver.validate_candidate(solver.solve_template("B", layout=layout))
        self.assertEqual(valid.status, "complete")
        assert valid.state is not None
        replayed_position = dict(valid.state.positioned_positions)["A"]
        different_legal_position = 4 if replayed_position != 4 else 3
        invalid_state = replace(
            valid.state,
            positioned_positions=(("A", different_legal_position),),
        )
        rejected = solver.validate_candidate(
            replace(valid, state=invalid_state, cost=(0, 0, 0, 0))
        )
        chosen = solver.choose_result([rejected, valid])

        self.assertEqual(rejected.status, "partial")
        self.assertTrue(
            any("candidate_state_replay_mismatch" in reason for reason in rejected.reasons)
        )
        self.assertEqual(chosen, valid)

    def test_blocking_inner_gets_do_not_suppress_put_neighbors(self) -> None:
        solver = object.__new__(Stage3Solver)
        state = State(lines=(), held=("H",), loco=("联7",), phase=1)
        after_get = State(lines=(), held=("H", "B"), loco=("修1库内",), phase=1)
        after_put = State(lines=(), held=(), loco=("修1库外",), phase=1)
        get_op = Op("Get", "修1库内", ("B",), ("联7", "修1库内"), ("H", "B"))
        put_op = Op("Put", "修1库外", ("H",), ("联7", "修1库外"), ())

        with (
            patch.object(
                solver,
                "blocking_inner_get_prefixes",
                return_value=[("修1库内", ("B",))],
            ),
            patch.object(solver, "apply_get", return_value=(get_op, after_get, "")),
            patch.object(
                solver,
                "put_suffixes",
                return_value=[("修1库外", ("H",))],
            ),
            patch.object(solver, "apply_put", return_value=(put_op, after_put, "")),
            patch.object(solver, "get_prefixes", return_value=[]),
        ):
            neighbors = list(solver.neighbors(state, "B"))

        legal_actions = {op.action for op, _next_state, reject in neighbors if not reject}
        self.assertEqual(legal_actions, {"Get", "Put"})

    def test_transaction_neighbors_keep_physically_legal_split_actions(self) -> None:
        solver = object.__new__(Stage3Solver)
        solver.line_map_cache = {}
        solver.assigned_line_by_no = {
            "S": "修2库内",
            "D": "修2库内",
            "G1": "修1库外",
            "G2": "修2库外",
            "G3": "修3库外",
        }
        solver.assigned_slot_by_no = {
            "S": ("修2库内", 1),
            "D": ("修2库内", 4),
        }
        state = State(
            lines=(("修2库外", ("G1", "G2", "G3")),),
            held=("S", "D"),
            loco=("联7",),
            phase=1,
        )
        after_get = replace(
            state,
            lines=(("修2库外", ("G3",)),),
            held=("S", "D", "G1", "G2"),
        )
        after_put = replace(state, held=("S",))
        partial_mixed_get = Op("Get", "修2库外", ("G1", "G2"), (), after_get.held)
        deepest_tail_put = Op("Put", "修2库内", ("D",), (), after_put.held)

        with patch.object(
            solver,
            "neighbors",
            return_value=[
                (partial_mixed_get, after_get, ""),
                (deepest_tail_put, after_put, ""),
            ],
        ):
            transitions = list(solver.transaction_neighbors(state, "B"))

        self.assertEqual(
            [(item.action.action, item.action.move) for item in transitions],
            [("Get", ("G1", "G2")), ("Put", ("D",))],
        )

    def test_transaction_progress_prevents_deeper_closure_starvation(self) -> None:
        solver = object.__new__(Stage3Solver)
        solver.active_nos = {"A", "B"}
        solver.assigned_line_by_no = {
            "A": "修1库内",
            "B": "修2库内",
        }
        solver.line_map_cache = {}
        shallow = State(
            lines=(("卸轮线", ("A", "B")),),
            held=(),
            loco=("联7",),
            phase=1,
        )
        deeper_but_trapped = State(
            lines=(("修1库内", ("B",)),),
            held=(),
            loco=("联7",),
            phase=1,
        )

        shallow_key = solver.transaction_progress_key(shallow, frozenset())
        deeper_key = solver.transaction_progress_key(
            deeper_but_trapped,
            frozenset({"A"}),
        )

        self.assertEqual(shallow_key[0], 2)
        self.assertEqual(deeper_key[0], 1)
        self.assertGreater(deeper_key[1], shallow_key[1])
        self.assertLess(deeper_key, shallow_key)

    def test_staging_affinity_prefers_near_term_matching_batch(self) -> None:
        cars = [
            car("S", "机走北", 1, ["修1库内"], forced=[1]),
            car("L", "机走北", 2, ["修3库内"], forced=[1]),
            car("N", "机走北", 3, ["修4库内"], forced=[1]),
        ]
        solver = Stage3Solver("TEST", request(cars), EMPTY_STAGE2, time_budget_seconds=5)
        solver.active_nos = {"S", "L", "N"}
        solver.assigned_line_by_no = {
            "S": "修1库内",
            "L": "修3库内",
            "N": "修4库内",
        }
        held = ("L", "N")
        stage_with_later_batch = State(
            lines=(("修3库外", ("S",)),),
            held=held,
            loco=("联7",),
            phase=1,
        )
        stage_with_next_batch = replace(
            stage_with_later_batch,
            lines=(("修4库外", ("S",)),),
        )

        later_debt = solver.staging_affinity_debt(
            stage_with_later_batch,
            frozenset(),
        )
        next_debt = solver.staging_affinity_debt(
            stage_with_next_batch,
            frozenset(),
        )

        self.assertLess(next_debt, later_debt)

    def test_alignment_debt_tracks_a_relevant_legal_tail_put(self) -> None:
        cars = [
            car("D", "机走北", 1, ["修1库内"], forced=[5]),
            car("X", "机走北", 2, ["修2库外"]),
        ]
        solver = Stage3Solver("TEST", request(cars), EMPTY_STAGE2, time_budget_seconds=5)
        solver.active_nos = {"D", "X"}
        solver.assigned_line_by_no = {
            "D": "修1库内",
            "X": "修2库外",
        }
        solver.assigned_slot_by_no = {"D": ("修1库内", 5)}
        state = State(
            lines=(),
            held=("D", "X"),
            loco=("联7",),
            phase=1,
        )
        before = solver.alignment_debt(state, frozenset())

        _op, after, reject = solver.apply_put(state, "卸轮线", ("X",))

        self.assertEqual(reject, "")
        self.assertEqual(before, 1)
        self.assertEqual(solver.alignment_debt(after, frozenset()), 0)
        committed = frozenset({"D"})
        self.assertEqual(
            solver.alignment_debt(after, committed),
            solver.alignment_debt(state, committed),
        )

    def test_pickup_merge_batches_a_flexible_exposed_car(self) -> None:
        cars = [
            car("H", "机走北", 1, ["修1库外"]),
            car("C", "修4库内", 1, ["油漆线"]),
        ]
        solver = Stage3Solver("TEST", request(cars), EMPTY_STAGE2, time_budget_seconds=5)
        solver.active_nos = {"H", "C"}
        solver.assigned_line_by_no = {
            "H": "修1库外",
            "C": "卸轮线",
        }
        solver.assigned_slot_by_no = {}
        state = State(
            lines=(("修4库内", ("C",)),),
            held=("H",),
            loco=("修4库外",),
            phase=1,
            positioned_positions=(("C", 1),),
        )
        get_op, after_get, reject = solver.apply_get(
            state,
            "修4库内",
            ("C",),
        )

        candidates = solver.pickup_merge_transactions(
            state,
            frozenset(),
            (transactions.LegalTransition(get_op, after_get),),
        )

        self.assertEqual(reject, "")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            [(op.action, op.line, op.move) for op in candidates[0].actions],
            [
                ("Get", "修4库内", ("C",)),
                ("Put", "修1库外", ("H", "C")),
            ],
        )
        self.assertEqual(candidates[0].newly_committed, frozenset({"H", "C"}))

    def test_compact_outer_projection_preserves_relative_not_absolute_position(self) -> None:
        solver = object.__new__(Stage3Solver)
        solver.line_map_cache = {}
        solver.assigned_slot_by_no = {}
        before = State(
            lines=(("修1库外", ("A", "B")),),
            held=(),
            loco=("联7",),
            phase=1,
            positioned_positions=(("A", 1), ("B", 2)),
        )
        after_prefix_insert = replace(
            before,
            lines=(("修1库外", ("X", "A", "B")),),
            positioned_positions=(("A", 2), ("B", 3), ("X", 1)),
        )
        committed = frozenset({"A", "B"})

        self.assertEqual(
            solver.committed_projection(before, committed),
            solver.committed_projection(after_prefix_insert, committed),
        )

        solver.assigned_slot_by_no = {"A": ("修1库外", 1)}
        self.assertNotEqual(
            solver.committed_projection(before, committed),
            solver.committed_projection(after_prefix_insert, committed),
        )

    def test_apply_get_requires_the_exposed_prefix_and_updates_state(self) -> None:
        req = request([
            car("A", "修1库外", 1, ["修2库外"]),
            car("B", "修1库外", 2, ["修3库外"]),
        ])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        state = solver.initial_state_without_pickup()

        _op, unchanged, reject = solver.apply_get(state, "修1库外", ("B",))
        self.assertEqual(reject, "get_order_violation")
        self.assertEqual(unchanged, state)

        with patch.object(
            solver,
            "route",
            return_value=(("存4线", "修1库外"), ""),
        ):
            op, next_state, reject = solver.apply_get(state, "修1库外", ("A",))

        self.assertEqual(reject, "")
        self.assertEqual(op.train_after, ("A",))
        self.assertEqual(next_state.held, ("A",))
        self.assertEqual(next_state.loco, ("修1库外",))
        self.assertEqual(dict(next_state.lines)["修1库外"], ("B",))
        self.assertNotIn("A", dict(next_state.positioned_positions))

    def test_apply_put_rejects_a_non_tail_block(self) -> None:
        req = request([
            car("A", "机走北", 1, ["修1库内"]),
            car("B", "机走北", 2, ["修1库内"]),
        ])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        state = State(lines=(), held=("A", "B"), loco=("联7",), phase=1)

        _op, unchanged, reject = solver.apply_put(state, "卸轮线", ("A",))

        self.assertEqual(reject, "put_tail_order_violation")
        self.assertEqual(unchanged, state)

    def test_mixed_gate_cycle_collects_remote_ready_block(self) -> None:
        cars = [
            car("H1", "机走北", 1, ["修1库内"], forced=[1]),
            car("H2", "机走北", 2, ["修1库内"], forced=[2]),
            car("G3", "修1库外", 1, ["修3库内"], forced=[3]),
            car("G4", "修1库外", 2, ["修3库内"], forced=[4]),
            car("S1", "修3库外", 1, ["修3库内"], forced=[1]),
            car("T4", "修3库外", 2, ["修1库内"], forced=[4]),
            car("T5", "修3库外", 3, ["修1库内"], forced=[5]),
            car("D", "修1库内", 5, ["油漆线"]),
            car("K", "机走北", 3, ["修2库内"], forced=[1]),
        ]
        solver = Stage3Solver("TEST", request(cars), EMPTY_STAGE2, time_budget_seconds=5)
        solver.active_nos = {item["No"] for item in cars}
        solver.fixed_cars = []
        solver.fixed_outer_lines = set()
        solver.fixed_positioned_positions = {}
        solver.assigned_line_by_no = {
            "H1": "修1库内", "H2": "修1库内", "T4": "修1库内", "T5": "修1库内",
            "S1": "修3库内", "G3": "修3库内", "G4": "修3库内",
            "D": "修1库外", "K": "修2库内",
        }
        solver.assigned_slot_by_no = {
            "H1": ("修1库内", 1), "H2": ("修1库内", 2),
            "T4": ("修1库内", 4), "T5": ("修1库内", 5),
            "S1": ("修3库内", 1), "G3": ("修3库内", 3),
            "G4": ("修3库内", 4), "K": ("修2库内", 1),
        }
        state = State(
            lines=solver.pack_lines({
                "修1库内": ("D",),
                "修1库外": ("G3", "G4"),
                "修2库内": ("K",),
                "修3库外": ("S1", "T4", "T5"),
            }),
            held=("H1", "H2"),
            loco=("修2库外",),
            phase=1,
            positioned_positions=(
                ("D", 5), ("G3", 1), ("G4", 2), ("K", 1),
                ("S1", 1), ("T4", 2), ("T5", 3),
            ),
        )
        before = solver.stable_closure(state, frozenset())

        transaction = min(
            (
                item
                for item in solver.mixed_gate_cycle_transactions(state, before)
                if item.newly_committed == frozenset({"T4", "T5"})
            ),
            key=lambda item: item.cost,
        )

        self.assertEqual(transaction.newly_committed, frozenset({"T4", "T5"}))
        self.assertEqual(
            [(op.action, op.line, op.move) for op in transaction.actions],
            [
                ("Get", "修1库外", ("G3", "G4")),
                ("Get", "修1库内", ("D",)),
                ("Get", "修3库外", ("S1", "T4", "T5")),
                ("Put", "修1库内", ("T4", "T5")),
            ],
        )
        projection = solver.committed_projection(state, before)
        working = state
        for op in transaction.actions:
            if op.action == "Get":
                _applied, working, reject = solver.apply_get(working, op.line, op.move)
            else:
                _applied, working, reject = solver.apply_put(working, op.line, op.move)
            self.assertEqual(reject, "")
            self.assertEqual(solver.committed_projection(working, before), projection)

    def test_mixed_gate_cycle_restages_gate_and_inner_blocker(self) -> None:
        held = [car(f"H{index}", "机走北", index, ["修4库内"], forced=[index]) for index in range(1, 5)]
        for item in held:
            item["IsHeavy"] = True
        blocker = car("D", "修1库内", 5, ["油漆线"])
        blocker["IsHeavy"] = True
        cars = [
            *held,
            car("G1", "修1库外", 1, ["修3库内"], forced=[1]),
            car("G2", "修1库外", 2, ["修3库内"], forced=[2]),
            car("T5", "修1库外", 3, ["修1库内"], forced=[5]),
            blocker,
        ]
        solver = Stage3Solver("TEST", request(cars), EMPTY_STAGE2, time_budget_seconds=5)
        solver.active_nos = {item["No"] for item in cars}
        solver.fixed_cars = []
        solver.fixed_outer_lines = set()
        solver.fixed_positioned_positions = {}
        solver.assigned_line_by_no = {
            **{f"H{index}": "修4库内" for index in range(1, 5)},
            "G1": "修3库内", "G2": "修3库内",
            "T5": "修1库内", "D": "修1库外",
        }
        solver.assigned_slot_by_no = {
            **{f"H{index}": ("修4库内", index) for index in range(1, 5)},
            "G1": ("修3库内", 1), "G2": ("修3库内", 2),
            "T5": ("修1库内", 5),
        }
        state = State(
            lines=solver.pack_lines({
                "修1库内": ("D",),
                "修1库外": ("G1", "G2", "T5"),
            }),
            held=("H1", "H2", "H3", "H4"),
            loco=("修2库外",),
            phase=1,
            positioned_positions=(("D", 5), ("G1", 1), ("G2", 2), ("T5", 3)),
        )

        with (
            patch.object(
                solver,
                "route",
                side_effect=lambda current, _action, line, _move: (
                    (current.loco[0], line),
                    "",
                ),
            ),
            patch.object(rv, "put_loco_positions", return_value={"联7"}),
        ):
            candidates = solver.mixed_gate_cycle_transactions(state, frozenset())
        restaged = next(
            item
            for item in candidates
            if [op.action for op in item.actions] == ["Get", "Put", "Get", "Put", "Get", "Put"]
        )

        self.assertEqual(restaged.newly_committed, frozenset({"D", "T5"}))
        self.assertEqual(restaged.actions[0].move, ("G1", "G2", "T5"))
        self.assertEqual(restaged.actions[2].move, ("D",))
        self.assertEqual(restaged.actions[-1].move, ("T5",))

    def test_mixed_gate_cycle_rejects_intermediate_committed_projection_change(self) -> None:
        cars = [
            car("C", "修1库外", 1, ["修2库内"], forced=[1]),
            car("T5", "修3库外", 1, ["修1库内"], forced=[5]),
            car("D", "修1库内", 5, ["油漆线"]),
        ]
        solver = Stage3Solver("TEST", request(cars), EMPTY_STAGE2, time_budget_seconds=5)
        solver.active_nos = {item["No"] for item in cars}
        solver.fixed_cars = []
        solver.fixed_outer_lines = set()
        solver.fixed_positioned_positions = {}
        solver.assigned_line_by_no = {
            "C": "修2库内",
            "T5": "修1库内",
            "D": "修1库外",
        }
        solver.assigned_slot_by_no = {
            "C": ("修2库内", 1),
            "T5": ("修1库内", 5),
        }
        state = State(
            lines=solver.pack_lines({
                "修1库内": ("D",),
                "修1库外": ("C",),
                "修3库外": ("T5",),
            }),
            held=(),
            loco=("修2库外",),
            phase=1,
            positioned_positions=(("C", 1), ("D", 5), ("T5", 1)),
        )

        self.assertEqual(
            solver.mixed_gate_cycle_transactions(state, frozenset({"C"})),
            (),
        )

    def test_mixed_gate_cycle_with_empty_gate_stages_inner_blocker(self) -> None:
        cars = [
            car("T5", "机走北", 1, ["修1库内"], forced=[5]),
            car("D", "修1库内", 5, ["油漆线"]),
        ]
        solver = Stage3Solver("TEST", request(cars), EMPTY_STAGE2, time_budget_seconds=5)
        solver.assigned_line_by_no = {
            "T5": "修1库内",
            "D": "修2库外",
        }
        solver.assigned_slot_by_no = {"T5": ("修1库内", 5)}
        state = State(
            lines=(("修1库内", ("D",)),),
            held=("T5",),
            loco=("修2库外",),
            phase=1,
            positioned_positions=(("D", 5),),
        )

        transaction = min(
            solver.mixed_gate_cycle_transactions(state, frozenset()),
            key=lambda item: item.cost,
        )

        self.assertEqual(transaction.newly_committed, frozenset({"D", "T5"}))
        self.assertEqual(
            [(op.action, op.move) for op in transaction.actions],
            [
                ("Get", ("D",)),
                ("Put", ("D",)),
                ("Put", ("T5",)),
            ],
        )
        self.assertNotEqual(transaction.actions[1].line, "修1库外")

    def test_mixed_gate_cycle_moves_terminal_but_unstable_inner_prefix(self) -> None:
        cars = [
            car("S1", "机走北", 1, ["修1库内"], forced=[1]),
            car("T5", "机走北", 2, ["修1库内"], forced=[5]),
        ]
        solver = Stage3Solver("TEST", request(cars), EMPTY_STAGE2, time_budget_seconds=5)
        solver.assigned_line_by_no = {
            "S1": "修1库内",
            "T5": "修1库内",
        }
        solver.assigned_slot_by_no = {
            "S1": ("修1库内", 1),
            "T5": ("修1库内", 5),
        }
        state = State(
            lines=(("修1库内", ("S1",)),),
            held=("T5",),
            loco=("修2库外",),
            phase=1,
            positioned_positions=(("S1", 1),),
        )
        before = solver.stable_closure(state, frozenset())

        transaction = min(
            solver.mixed_gate_cycle_transactions(state, before),
            key=lambda item: item.cost,
        )

        self.assertTrue(solver.terminal_line_satisfied("S1", "修1库内"))
        self.assertNotIn("S1", before)
        self.assertEqual(transaction.newly_committed, frozenset({"T5"}))
        self.assertEqual(
            [(op.action, op.move) for op in transaction.actions],
            [
                ("Get", ("S1",)),
                ("Put", ("S1",)),
                ("Put", ("T5",)),
            ],
        )

    def test_feasibility_potential_bounds_hook_cost_per_commit(self) -> None:
        req = request([
            car("A", "机走北", 1, ["修1库内"]),
            car("B", "机走北", 2, ["修1库内"]),
            car("C", "机走北", 3, ["修1库内"]),
        ])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        one = Op("Put", "修1库内", ("A",), (), ())
        expensive = transactions.Transaction(
            start_state="root",
            end_state="high_progress",
            actions=(one, one, one, one),
            committed_before=frozenset(),
            committed_after=frozenset({"A", "B"}),
            cost=(4, 0, 0, 0),
        )
        cheap = transactions.Transaction(
            start_state="root",
            end_state="low_cost",
            actions=(one,),
            committed_before=frozenset(),
            committed_after=frozenset({"A"}),
            cost=(1, 0, 0, 0),
        )
        expensive_finish = transactions.Transaction(
            start_state="high_progress",
            end_state="expensive_done",
            actions=(one,),
            committed_before=frozenset({"A", "B"}),
            committed_after=frozenset({"A", "B", "C"}),
            cost=(1, 0, 0, 0),
        )
        cheap_finish = replace(
            expensive_finish,
            start_state="low_cost",
            end_state="cheap_done",
            committed_before=frozenset({"A"}),
        )
        closure = {
            "root": frozenset(),
            "high_progress": frozenset({"A", "B"}),
            "low_cost": frozenset({"A"}),
            "expensive_done": frozenset({"A", "B", "C"}),
            "cheap_done": frozenset({"A", "B", "C"}),
        }
        lower_bound = {
            "root": 2,
            "high_progress": 1,
            "low_cost": 1,
            "expensive_done": 0,
            "cheap_done": 0,
        }
        solver.active_nos = {"A", "B", "C"}

        with (
            patch.object(
                solver,
                "stable_closure",
                side_effect=lambda state, seed: seed | closure[state],
            ),
            patch.object(
                solver,
                "transaction_hook_lower_bound",
                side_effect=lambda state, _committed: lower_bound[state],
            ),
            patch.object(
                solver,
                "transaction_progress_key",
                side_effect=lambda state, _committed: (
                    len(solver.active_nos - closure[state]),
                    0,
                    0,
                    0,
                    0,
                ),
            ),
            patch.object(
                solver,
                "complete",
                side_effect=lambda state: state.endswith("_done"),
            ),
            patch.object(solver, "deadline_reached", return_value=False),
            patch.object(
                solver,
                "unsatisfied_active_count",
                side_effect=lambda state: len(solver.active_nos - closure[state]),
            ),
            patch.object(
                solver,
                "direct_transfer_transactions",
                side_effect=lambda state, _committed: {
                    "root": (cheap, expensive),
                    "high_progress": (expensive_finish,),
                    "low_cost": (cheap_finish,),
                }.get(state, ()),
            ),
            patch.object(solver, "direct_put_transactions", return_value=()),
            patch.object(solver, "capacity_exchange_transactions", return_value=()),
            patch.object(solver, "gate_chain_transactions", return_value=()),
            patch.object(solver, "mixed_gate_cycle_transactions", return_value=()),
            patch.object(solver, "transaction_neighbors", return_value=()),
        ):
            result = solver.plan_transactions(
                "B",
                "root",  # type: ignore[arg-type]
                (),
                solver.started_at,
                expansion_limit=3,
            )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.state, "cheap_done")
        self.assertEqual(result.cost, (2, 0, 0, 0))
        self.assertEqual(len(result.ops), 2)
        self.assertEqual(result.expansions, 2)

    def test_primitive_lowering_hook_bound_is_preferred_by_guide(self) -> None:
        req = request([car("A", "机走北", 1, ["修1库内"])])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        phase = Op("Phase", "", (), (), ())
        hook = Op("Put", "修1库内", ("A",), (), ())
        neighbors = {
            "root": (
                transactions.LegalTransition(phase, "ordinary"),
                transactions.LegalTransition(hook, "lower_bound_drop"),
            ),
            "lower_bound_drop": (
                transactions.LegalTransition(hook, "done"),
            ),
        }
        lower_bound = {
            "root": 2,
            "ordinary": 2,
            "lower_bound_drop": 1,
            "done": 0,
        }

        with (
            patch.object(solver, "stable_closure", return_value=frozenset()),
            patch.object(
                solver,
                "transaction_hook_lower_bound",
                side_effect=lambda state, _committed: lower_bound[state],
            ),
            patch.object(
                solver,
                "transaction_progress_key",
                side_effect=lambda state, _committed: (
                    int(state != "done"),
                    0,
                    0,
                    0,
                    0,
                ),
            ),
            patch.object(solver, "complete", side_effect=lambda state: state == "done"),
            patch.object(solver, "deadline_reached", return_value=False),
            patch.object(solver, "unsatisfied_active_count", return_value=1),
            patch.object(solver, "direct_put_transactions", return_value=()),
            patch.object(solver, "direct_transfer_transactions", return_value=()),
            patch.object(solver, "capacity_exchange_transactions", return_value=()),
            patch.object(solver, "gate_chain_transactions", return_value=()),
            patch.object(solver, "mixed_gate_cycle_transactions", return_value=()),
            patch.object(
                solver,
                "transaction_neighbors",
                side_effect=lambda state, _template: neighbors.get(state, ()),
            ),
        ):
            result = solver.plan_transactions(
                "B",
                "root",  # type: ignore[arg-type]
                (),
                solver.started_at,
                expansion_limit=2,
            )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.state, "done")
        self.assertEqual(result.cost, (2, 0, 0, 0))
        self.assertEqual(result.expansions, 2)

    def test_put_get_put_is_kept_when_it_reaches_a_distinct_state(self) -> None:
        req = request([car("X", "机走北", 1, ["修1库内"])])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        solver.active_nos = {"X"}
        put = Op("Put", "修1库内", ("X",), (), ())
        get = Op("Get", "修1库内", ("X",), (), ())
        root = State((), ("X",), ("root",), 0)
        put_once = State((), (), ("put_once",), 0)
        get_again = State((), ("X",), ("get_again",), 0)
        done = State((), (), ("done",), 1)
        closure = {
            root: frozenset(),
            put_once: frozenset(),
            get_again: frozenset(),
            done: frozenset({"X"}),
        }
        neighbors = {
            root: (transactions.LegalTransition(put, put_once),),
            put_once: (transactions.LegalTransition(get, get_again),),
            get_again: (transactions.LegalTransition(put, done),),
        }

        with (
            patch.object(
                solver,
                "stable_closure",
                side_effect=lambda state, seed: seed | closure[state],
            ),
            patch.object(solver, "transaction_hook_lower_bound", return_value=0),
            patch.object(
                solver,
                "transaction_progress_key",
                side_effect=lambda state, _committed: (
                    len(solver.active_nos - closure[state]),
                    0,
                    0,
                    0,
                    0,
                ),
            ),
            patch.object(solver, "complete", side_effect=lambda state: state == done),
            patch.object(solver, "deadline_reached", return_value=False),
            patch.object(solver, "unsatisfied_active_count", return_value=1),
            patch.object(solver, "direct_put_transactions", return_value=()),
            patch.object(solver, "direct_transfer_transactions", return_value=()),
            patch.object(solver, "capacity_exchange_transactions", return_value=()),
            patch.object(solver, "gate_chain_transactions", return_value=()),
            patch.object(solver, "mixed_gate_cycle_transactions", return_value=()),
            patch.object(
                solver,
                "transaction_neighbors",
                side_effect=lambda state, _template: neighbors.get(state, ()),
            ),
        ):
            result = solver.plan_transactions(
                "B",
                root,
                (),
                solver.started_at,
                expansion_limit=3,
            )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.state, done)
        self.assertEqual([op.action for op in result.ops], ["Put", "Get", "Put"])

    def test_anchor_recovers_after_guide_drains(self) -> None:
        req = request([car("X", "机走北", 1, ["修1库内"])])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        solver.active_nos = {"X"}
        put = Op("Put", "修1库内", ("X",), (), ())
        root = State((), ("X",), ("root",), 0)
        detour = State((), ("X",), ("detour",), 0)
        necessary = State((), ("X",), ("necessary",), 0)
        done = State((), (), ("done",), 1)
        closure = {
            root: frozenset(),
            detour: frozenset(),
            necessary: frozenset(),
            done: frozenset({"X"}),
        }
        structural = transactions.Transaction(
            start_state=root,
            end_state=detour,
            actions=(put, put, put),
            committed_before=frozenset(),
            committed_after=frozenset(),
            cost=(3, 0, 0, 0),
        )
        neighbors = {
            root: (transactions.LegalTransition(put, necessary),),
            necessary: (transactions.LegalTransition(put, done),),
        }

        with (
            patch.object(
                solver,
                "stable_closure",
                side_effect=lambda state, seed: seed | closure[state],
            ),
            patch.object(solver, "transaction_hook_lower_bound", return_value=0),
            patch.object(
                solver,
                "transaction_progress_key",
                side_effect=lambda state, _committed: (
                    len(solver.active_nos - closure[state]),
                    0,
                    0,
                    0,
                    0,
                ),
            ),
            patch.object(solver, "complete", side_effect=lambda state: state == done),
            patch.object(solver, "deadline_reached", return_value=False),
            patch.object(solver, "unsatisfied_active_count", return_value=1),
            patch.object(solver, "direct_put_transactions", return_value=()),
            patch.object(
                solver,
                "direct_transfer_transactions",
                side_effect=lambda state, _committed: (
                    (structural,) if state == root else ()
                ),
            ),
            patch.object(solver, "capacity_exchange_transactions", return_value=()),
            patch.object(solver, "gate_chain_transactions", return_value=()),
            patch.object(solver, "mixed_gate_cycle_transactions", return_value=()),
            patch.object(
                solver,
                "transaction_neighbors",
                side_effect=lambda state, _template: neighbors.get(state, ()),
            ),
        ):
            result = solver.plan_transactions(
                "B",
                root,
                (),
                solver.started_at,
                expansion_limit=3,
            )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.state, done)
        self.assertEqual(result.cost, (2, 0, 0, 0))
        self.assertEqual(result.expansions, 3)

    def test_generated_complete_is_retained_before_next_frontier_pop(self) -> None:
        req = request([car("A", "机走北", 1, ["修1库内"])])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        solver.active_nos = {"A"}
        op = Op("Put", "修1库内", ("A",), (), ())
        goal = transactions.Transaction(
            start_state="root",
            end_state="done",
            actions=(op,),
            committed_before=frozenset(),
            committed_after=frozenset({"A"}),
            cost=(1, 0, 0, 0),
        )
        closure = {"root": frozenset(), "done": frozenset({"A"})}

        with (
            patch.object(
                solver,
                "stable_closure",
                side_effect=lambda state, seed: seed | closure[state],
            ),
            patch.object(
                solver,
                "transaction_hook_lower_bound",
                side_effect=lambda state, _committed: int(state == "root"),
            ),
            patch.object(
                solver,
                "transaction_progress_key",
                side_effect=lambda state, _committed: (
                    len(solver.active_nos - closure[state]),
                    0,
                    0,
                    0,
                    0,
                ),
            ),
            patch.object(
                solver,
                "complete",
                side_effect=lambda state: state == "done",
            ),
            patch.object(solver, "deadline_reached", side_effect=[False, True]),
            patch.object(
                solver,
                "unsatisfied_active_count",
                side_effect=lambda state: len(solver.active_nos - closure[state]),
            ),
            patch.object(
                solver,
                "direct_put_transactions",
                side_effect=lambda state, _committed: (goal,) if state == "root" else (),
            ),
            patch.object(solver, "direct_transfer_transactions", return_value=()),
            patch.object(solver, "capacity_exchange_transactions", return_value=()),
            patch.object(solver, "gate_chain_transactions", return_value=()),
            patch.object(solver, "mixed_gate_cycle_transactions", return_value=()),
            patch.object(solver, "transaction_neighbors", return_value=()),
        ):
            result = solver.plan_transactions(
                "B",
                "root",  # type: ignore[arg-type]
                (),
                solver.started_at,
                expansion_limit=1,
            )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.state, "done")
        self.assertEqual(result.cost, (1, 0, 0, 0))

    def test_transaction_search_resumes_at_cumulative_expansion_limit(self) -> None:
        solver = Stage3Solver(
            "TEST",
            request([
                car("A", "机走北", 1, ["修1库内"]),
                car("B", "机走北", 2, ["修1库内"]),
            ]),
            EMPTY_STAGE2,
            time_budget_seconds=5,
        )
        solver.active_nos = {"A", "B"}
        put_a = Op("Put", "修1库内", ("A",), (), ())
        put_b = Op("Put", "修1库内", ("B",), (), ())
        closure = {
            "root": frozenset(),
            "mid": frozenset({"A"}),
            "done": frozenset({"A", "B"}),
        }
        neighbors = {
            "root": (transactions.LegalTransition(put_a, "mid"),),
            "mid": (transactions.LegalTransition(put_b, "done"),),
        }
        expanded_states: list[str] = []

        def transitions(state: str, _template: str) -> tuple[object, ...]:
            expanded_states.append(state)
            return neighbors.get(state, ())

        with (
            patch.object(
                solver,
                "stable_closure",
                side_effect=lambda state, seed: seed | closure[state],
            ),
            patch.object(
                solver,
                "transaction_hook_lower_bound",
                side_effect=lambda state, _committed: len(
                    solver.active_nos - closure[state]
                ),
            ),
            patch.object(
                solver,
                "transaction_progress_key",
                side_effect=lambda state, _committed: (
                    len(solver.active_nos - closure[state]),
                    0,
                    0,
                    0,
                    0,
                ),
            ),
            patch.object(solver, "complete", side_effect=lambda state: state == "done"),
            patch.object(solver, "deadline_reached", return_value=False),
            patch.object(
                solver,
                "unsatisfied_active_count",
                side_effect=lambda state: len(solver.active_nos - closure[state]),
            ),
            patch.object(solver, "direct_put_transactions", return_value=()),
            patch.object(solver, "direct_transfer_transactions", return_value=()),
            patch.object(solver, "capacity_exchange_transactions", return_value=()),
            patch.object(solver, "gate_chain_transactions", return_value=()),
            patch.object(solver, "mixed_gate_cycle_transactions", return_value=()),
            patch.object(solver, "transaction_neighbors", side_effect=transitions),
        ):
            first = solver.plan_transactions(
                "B",
                "root",  # type: ignore[arg-type]
                (),
                solver.started_at,
                expansion_limit=1,
                search_key=("B", "resume-test"),
            )
            resumed = solver.plan_transactions(
                "B",
                "root",  # type: ignore[arg-type]
                (),
                solver.started_at,
                expansion_limit=2,
                search_key=("B", "resume-test"),
            )

        self.assertEqual(first.status, "partial")
        self.assertEqual(first.expansions, 1)
        self.assertEqual(resumed.status, "complete")
        self.assertEqual(resumed.expansions, 2)
        self.assertEqual(expanded_states, ["root", "mid"])

    def test_restoration_position_shift_does_not_add_a_false_get_bound(self) -> None:
        solver = Stage3Solver(
            "TEST",
            request([
                car("B", "机走北", 1, ["机走北"]),
                car("R", "机走北", 2, ["机走北"]),
            ]),
            EMPTY_STAGE2,
            time_budget_seconds=5,
        )
        solver.active_nos = {"B", "R"}
        solver.restoration_nos = {"B", "R"}
        solver.restoration_position_nos = {"B", "R"}
        solver.assigned_line_by_no = {"B": "机走北", "R": "机走北"}
        state = State(
            lines=solver.pack_lines({"机走北": ("R",)}),
            held=("B",),
            loco=("机走北",),
            phase=1,
        )

        self.assertEqual(
            solver.transaction_hook_lower_bound(state, frozenset()),
            1,
        )

    def test_budget_exhaustion_returns_existing_incumbent(self) -> None:
        req = request([
            car("A", "机走北", 1, ["修1库内"]),
            car("B", "机走北", 2, ["修1库内"]),
        ])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        solver.active_nos = {"A", "B"}
        op = Op("Put", "修1库内", ("A",), (), ())
        goal = transactions.Transaction(
            start_state="root",
            end_state="done",
            actions=(op, op, op),
            committed_before=frozenset(),
            committed_after=frozenset({"A", "B"}),
            cost=(3, 0, 0, 0),
        )
        dead = tuple(
            transactions.Transaction(
                start_state="root",
                end_state=f"dead_{index}",
                actions=(op,),
                committed_before=frozenset(),
                committed_after=frozenset({"A"}),
                cost=(1, 0, 0, 0),
            )
            for index in range(3)
        )
        root_candidates = (goal, *dead)
        closure = {
            "root": frozenset(),
            "done": frozenset({"A", "B"}),
            **{f"dead_{index}": frozenset({"A"}) for index in range(3)},
        }

        with (
            patch.object(
                solver,
                "stable_closure",
                side_effect=lambda state, seed: seed | closure[state],
            ),
            patch.object(
                solver,
                "transaction_hook_lower_bound",
                return_value=0,
            ),
            patch.object(
                solver,
                "transaction_progress_key",
                side_effect=lambda state, _committed: (
                    len(solver.active_nos - closure[state]),
                    0,
                    0,
                    0,
                    0,
                ),
            ),
            patch.object(
                solver,
                "complete",
                side_effect=lambda state: state == "done",
            ),
            patch.object(solver, "deadline_reached", return_value=False),
            patch.object(
                solver,
                "unsatisfied_active_count",
                side_effect=lambda state: len(solver.active_nos - closure[state]),
            ),
            patch.object(
                solver,
                "direct_transfer_transactions",
                side_effect=lambda state, _committed: (
                    root_candidates if state == "root" else ()
                ),
            ),
            patch.object(solver, "direct_put_transactions", return_value=()),
            patch.object(solver, "capacity_exchange_transactions", return_value=()),
            patch.object(solver, "gate_chain_transactions", return_value=()),
            patch.object(solver, "mixed_gate_cycle_transactions", return_value=()),
            patch.object(solver, "transaction_neighbors", return_value=()),
        ):
            result = solver.plan_transactions(
                "B",
                "root",  # type: ignore[arg-type]
                (),
                solver.started_at,
                expansion_limit=1,
            )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.state, "done")
        self.assertEqual(result.cost, (3, 0, 0, 0))
        self.assertFalse(result.search_spec_evaluated)

    def test_partial_response_drops_open_train_operation_prefix(self) -> None:
        req = request([car("A", "机走北", 1, ["修1库内"])])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        layout = self.prepare_unified_assignment(solver)
        open_get = Op(
            "Get",
            "机走北",
            ("A",),
            ("存4线", "渡1", "联6", "渡2", "机北1", "机北2", "渡5", "机走北"),
            ("A",),
        )
        chosen = SearchResult(
            status="partial",
            template="B",
            state=None,
            ops=(open_get,),
            cost=(10**9, 0, 0, 0),
            reasons=("stage3_global_time_budget_exhausted",),
            expansions=1,
            elapsed_seconds=5.0,
            layout=layout,
            deferred_clear=False,
            inner_clear_policy="transaction",
            search_spec_evaluated=False,
        )

        result = solver.result(chosen, [chosen])
        operations = result["response"]["Data"]["Operations"]
        _replayed, violations = rv.replay(result["stage3_request"], result["response"])

        self.assertEqual(operations, [])
        self.assertFalse(
            any(item.code == "dirty_train_after_last_operation" for item in violations)
        )

    def test_closed_partial_response_exposes_no_generated_state(self) -> None:
        req = request([car("A", "机走北", 1, ["修1库内"])])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        layout = self.prepare_unified_assignment(solver)
        attempted = Op("Get", "机走北", ("A",), (), ("A",))
        chosen = SearchResult(
            status="partial",
            template="B",
            state=solver.initial_state_without_pickup(),
            ops=(attempted,),
            cost=(10**9, 0, 0, 0),
            reasons=("no_strict_progress_transaction",),
            expansions=1,
            elapsed_seconds=0.0,
            layout=layout,
            deferred_clear=False,
            inner_clear_policy="transaction",
        )

        result = solver.result(chosen, [chosen])

        self.assertEqual(
            result["response"]["Data"],
            {"Operations": [], "GeneratedEndStatus": []},
        )
        self.assertEqual(result["summary"]["business_hooks"], 0)
        self.assertEqual(result["summary"]["attempted_operations"], 1)
        self.assertTrue(
            any(
                item["code"] == "target_line_unsatisfied"
                for item in result["summary"]["residual_business_violations"]
            )
        )
        self.assertEqual(len(result["trace"]), 1)

    def test_partial_choice_prefers_stable_progress_but_exposes_zero_hooks(self) -> None:
        req = request([car("A", "机走北", 1, ["修1库内"])])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        layout = self.prepare_unified_assignment(solver)
        state = solver.initial_state_without_pickup()
        attempted = Op("Get", "机走北", ("A",), (), ("A",))
        shallower = SearchResult(
            status="partial",
            template="A",
            state=state,
            ops=(attempted, attempted),
            cost=(10**9, 0, 0, 0),
            reasons=("shallower",),
            expansions=100,
            elapsed_seconds=1.0,
            layout=layout,
            committed_count=0,
        )
        deeper = replace(
            shallower,
            template="B",
            ops=(attempted,),
            reasons=("deeper", "diagnostic_detail"),
            expansions=10,
            committed_count=1,
        )
        expensive_same_progress = replace(
            deeper,
            template="A",
            ops=(attempted, attempted, attempted),
            reasons=("same_progress_more_hooks",),
            expansions=1_000,
        )

        chosen = solver.choose_result([shallower, expensive_same_progress, deeper])
        result = solver.result(
            chosen,
            [shallower, expensive_same_progress, deeper],
        )

        self.assertIs(chosen, deeper)
        self.assertEqual(
            result["response"]["Data"],
            {"Operations": [], "GeneratedEndStatus": []},
        )
        self.assertEqual(result["summary"]["business_hooks"], 0)
        self.assertEqual(result["summary"]["attempted_operations"], 1)
        self.assertEqual(result["summary"]["stable_committed_count"], 1)

    def test_partial_choice_uses_budgeted_frontier_obstruction_before_hooks(self) -> None:
        req = request([car("A", "机走北", 1, ["修1库内"])])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        state = solver.initial_state_without_pickup()
        attempted = Op("Get", "机走北", ("A",), (), ("A",))
        blocked = SearchResult(
            status="partial",
            template="B",
            state=state,
            ops=(attempted,),
            cost=(10**9, 0, 0, 0),
            reasons=("blocked",),
            expansions=100,
            elapsed_seconds=0.0,
            committed_count=0,
            budgeted_progress_key=(1, 1, 0, 0, 0),
        )
        aligned = replace(
            blocked,
            template="A",
            ops=(attempted, attempted),
            reasons=("aligned",),
            budgeted_progress_key=(1, 0, 2, 0, 0),
        )

        self.assertIs(solver.choose_result([blocked, aligned]), aligned)

    def test_select_finalist_specs_deduplicates_static_signatures_and_degrades(self) -> None:
        solver = object.__new__(Stage3Solver)
        solver.active_nos = {"A"}

        def spec(index: int, line: str) -> PlacementSpec:
            return PlacementSpec(
                operation_lower_bound=1,
                placement_score=(index,),
                signature=(("A", "inner", line, 1),),
                template="B",
                layout=f"unified:B:{index:02d}",
            )

        first = spec(0, "修1库内")
        same_signature = replace(first, layout="unified:B:01")
        second = replace(spec(2, "修2库内"), placement_score=(1,))
        dynamic = replace(spec(3, "修3库内"), placement_score=(2,))
        base = SearchResult(
            status="partial",
            template="B",
            state=None,
            ops=(),
            cost=(10**9, 0, 0, 0),
            reasons=("budget",),
            expansions=25,
            elapsed_seconds=0.0,
        )

        results = {
            (item.template, item.layout): replace(
                base,
                layout=item.layout,
                committed_count=int(item is dynamic),
            )
            for item in (first, same_signature, second, dynamic)
        }

        selected = solver.select_finalist_specs(
            [first, same_signature, second, dynamic],
            results,
        )

        self.assertEqual(selected, [first, second, dynamic])
        self.assertEqual(
            solver.select_finalist_specs([first], results),
            [first],
        )
        self.assertEqual(solver.select_finalist_specs([], results), [])

    def test_select_finalist_specs_covers_pickup_templates_before_filling(self) -> None:
        solver = object.__new__(Stage3Solver)

        def spec(template: str, index: int) -> PlacementSpec:
            return PlacementSpec(
                operation_lower_bound=index,
                placement_score=(index,),
                signature=((f"{template}{index}", "outer", "修1库外", -1),),
                template=template,
                layout=f"unified:{template}:{index:02d}",
            )

        first_b = spec("B", 0)
        second_b = spec("B", 1)
        third_b = spec("B", 2)
        first_a = spec("A", 3)

        selected = solver.select_finalist_specs(
            [first_b, second_b, third_b, first_a],
            {},
        )

        self.assertEqual(selected, [first_b, first_a, second_b])
        self.assertEqual({item.template for item in selected}, {"A", "B"})

    def test_select_finalist_specs_prioritizes_less_deeply_searched_layouts(self) -> None:
        solver = object.__new__(Stage3Solver)

        def spec(index: int) -> PlacementSpec:
            return PlacementSpec(
                operation_lower_bound=1,
                placement_score=(index,),
                signature=((f"C{index}", "outer", "修1库外", -1),),
                template="B",
                layout=f"unified:B:{index:02d}",
            )

        specs = [spec(index) for index in range(4)]
        searched = SearchResult(
            status="complete",
            template="B",
            state=None,
            ops=(),
            cost=(1, 0, 0, 0),
            reasons=(),
            expansions=5_000,
            elapsed_seconds=0.0,
            search_spec_evaluated=False,
        )
        results = {
            (item.template, item.layout): replace(searched, layout=item.layout)
            for item in specs[:3]
        }

        selected = solver.select_finalist_specs(specs, results)

        self.assertEqual(selected[0], specs[3])
        self.assertEqual(selected, [specs[3]])

    def test_select_finalist_specs_exploits_low_hook_incumbent_after_screening(self) -> None:
        solver = object.__new__(Stage3Solver)
        hook = Op("Put", "修1库外", ("A",), (), ())

        def spec(index: int) -> PlacementSpec:
            return PlacementSpec(
                operation_lower_bound=5,
                placement_score=(index,),
                signature=((f"C{index}", "outer", "修1库外", -1),),
                template="B",
                layout=f"unified:B:{index:02d}",
            )

        specs = [spec(index) for index in range(4)]
        results = {
            (item.template, item.layout): SearchResult(
                status="complete",
                template=item.template,
                state=None,
                ops=(hook,) * (8 if index == 0 else 14),
                cost=(8 if index == 0 else 14, 0, 0, 0),
                reasons=(),
                expansions=5_000 if index == 0 else 100,
                elapsed_seconds=0.0,
                layout=item.layout,
                search_spec_evaluated=False,
            )
            for index, item in enumerate(specs)
        }

        selected = solver.select_finalist_specs(specs, results)

        self.assertEqual(selected[0], specs[0])
        self.assertEqual(len(selected), 3)

    def test_finalist_first_deepening_stops_at_screening_floor(self) -> None:
        prior = SearchResult(
            status="partial",
            template="B",
            state=None,
            ops=(),
            cost=(10**9, 0, 0, 0),
            reasons=("budget",),
            expansions=25,
            elapsed_seconds=0.0,
        )

        self.assertEqual(
            Stage3Solver.finalist_expansion_target(None, 250),
            25,
        )
        self.assertEqual(
            Stage3Solver.finalist_expansion_target(
                replace(prior, expansions=0),
                500,
            ),
            25,
        )
        self.assertEqual(
            Stage3Solver.finalist_expansion_target(
                prior,
                500,
            ),
            500,
        )

    def test_deepening_evaluates_lower_bound_viable_dynamic_finalist(self) -> None:
        req = request([car("A", "机走北", 1, ["修1库内"])])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        plans = tuple(
            placement.Plan(
                assignments=(
                    ("A", placement.Atom("inner", "修1库内", position)),
                ),
                score=(position,),
            )
            for position in range(1, 4)
        )
        available = placement.SolveResult(
            plans=plans,
            explored_nodes=3,
            complete=True,
            budget_exhausted=False,
            frontier_truncated=False,
        )
        unavailable = replace(
            available,
            plans=(),
            explored_nodes=0,
            reason="test_no_plans",
        )
        initial = solver.initial_state_without_pickup()
        attempted = Op("Get", "机走北", ("A",), (), ("A",))
        partial = SearchResult(
            status="partial",
            template="B",
            state=initial,
            ops=(attempted,),
            cost=(10**9, 0, 0, 0),
            reasons=("screening",),
            expansions=0,
            elapsed_seconds=0.0,
            search_spec_evaluated=False,
        )
        calls: list[tuple[str, int]] = []

        def unified_placements(template: str) -> placement.SolveResult:
            solved = available if template == "B" else unavailable
            solver.placement_candidates[template] = tuple(
                (
                    f"unified:{template}:{index:02d}",
                    plan,
                )
                for index, plan in enumerate(solved.plans)
            )
            return solved

        def solve_template(
            template: str,
            *,
            layout: str,
            transaction_expansion_limit: int,
        ) -> SearchResult:
            calls.append((layout, transaction_expansion_limit))
            candidate = replace(
                partial,
                template=template,
                layout=layout,
                expansions=transaction_expansion_limit,
            )
            complete_cost = None
            if transaction_expansion_limit >= 5_000:
                complete_cost = {
                    "unified:B:00": 30,
                    "unified:B:01": 20,
                }.get(layout)
            if layout == "unified:B:02" and transaction_expansion_limit >= 10_000:
                complete_cost = 15
            if complete_cost is None:
                return candidate
            return replace(
                candidate,
                status="complete",
                ops=(attempted,) * complete_cost,
                cost=(complete_cost, 0, 0, 0),
                reasons=(),
                committed_count=1,
            )

        with (
            patch.object(
                solver,
                "unified_placements",
                side_effect=unified_placements,
            ),
            patch.object(
                solver,
                "build_assigned_line_by_no",
                return_value={"A": "修1库内"},
            ),
            patch.object(
                solver,
                "template_operation_lower_bound_components",
                return_value={"test": 0},
            ),
            patch.object(solver, "solve_template", side_effect=solve_template),
            patch.object(solver, "validate_candidate", side_effect=lambda item: item),
            patch.object(
                solver,
                "result",
                side_effect=lambda chosen, results: {"chosen": chosen, "results": results},
            ),
        ):
            result = solver.solve()

        self.assertEqual(
            [layout for layout, limit in calls if limit == 5_000],
            ["unified:B:00", "unified:B:01", "unified:B:02"],
        )
        self.assertEqual(
            [layout for layout, limit in calls if limit == 10_000],
            ["unified:B:01", "unified:B:00", "unified:B:02"],
        )
        self.assertEqual(result["chosen"].layout, "unified:B:02")
        self.assertEqual(result["chosen"].cost, (15, 0, 0, 0))
        self.assertEqual(
            {
                item.layout
                for item in result["results"]
                if item.status == "complete"
            },
            {"unified:B:00", "unified:B:01", "unified:B:02"},
        )

    def test_stop_certificate_covers_every_retained_placement(self) -> None:
        req = request([car("A", "机走北", 1, ["修1库内"])])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        plans = tuple(
            placement.Plan(
                assignments=(("A", placement.Atom("inner", "修1库内", position)),),
                score=(0, position),
            )
            for position in range(1, 5)
        )
        available = placement.SolveResult(
            plans=plans,
            explored_nodes=4,
            complete=True,
            budget_exhausted=False,
            frontier_truncated=False,
        )
        initial = solver.initial_state_without_pickup()
        attempted = Op("Get", "机走北", ("A",), (), ("A",))
        hooks_by_layout = {
            "unified:B:00": 30,
            "unified:B:01": 20,
            "unified:B:02": 18,
            "unified:B:03": 10,
        }
        calls: list[str] = []

        def unified_placements(template: str) -> placement.SolveResult:
            solver.placement_candidates[template] = tuple(
                (f"unified:{template}:{index:02d}", plan)
                for index, plan in enumerate(plans)
            )
            return available

        def solve_template(
            template: str,
            *,
            layout: str,
            transaction_expansion_limit: int,
        ) -> SearchResult:
            del transaction_expansion_limit
            calls.append(layout)
            hooks = hooks_by_layout[layout]
            return SearchResult(
                status="complete",
                template=template,
                state=initial,
                ops=(attempted,) * hooks,
                cost=(hooks, 0, 0, 0),
                reasons=(),
                expansions=1,
                elapsed_seconds=0.0,
                layout=layout,
                search_spec_evaluated=True,
                committed_count=1,
            )

        with (
            patch.object(solver, "unified_placements", side_effect=unified_placements),
            patch.object(
                solver,
                "build_assigned_line_by_no",
                return_value={"A": "修1库内"},
            ),
            patch.object(
                solver,
                "template_operation_lower_bound_components",
                return_value={"test": 0},
            ),
            patch.object(solver, "solve_template", side_effect=solve_template),
            patch.object(solver, "validate_candidate", side_effect=lambda item: item),
            patch.object(
                solver,
                "result",
                side_effect=lambda chosen, results: {
                    "chosen": chosen,
                    "results": results,
                },
            ),
        ):
            result = solver.solve()

        self.assertIn("unified:B:03", calls)
        self.assertEqual(result["chosen"].layout, "unified:B:03")
        self.assertEqual(result["chosen"].cost, (10, 0, 0, 0))

    def test_finalist_lower_bound_only_prunes_strictly_worse_hook_counts(self) -> None:
        solver = object.__new__(Stage3Solver)
        spec = PlacementSpec(
            operation_lower_bound=20,
            placement_score=(),
            signature=(),
            template="B",
            layout="unified:B:00",
        )

        self.assertTrue(
            solver.finalist_lower_bound_can_improve(spec, 20)
        )
        self.assertTrue(
            solver.finalist_lower_bound_can_improve(
                replace(spec, operation_lower_bound=19),
                20,
            )
        )
        self.assertFalse(
            solver.finalist_lower_bound_can_improve(
                replace(spec, operation_lower_bound=21),
                20,
            )
        )

    def test_iterative_deepening_uses_certified_bounds_before_finalists(self) -> None:
        req = request([car("A", "机走北", 1, ["修1库内"])])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        b_plans = tuple(
            placement.Plan(
                assignments=(("A", placement.Atom("inner", "修1库内", position)),),
                score=(10,),
            )
            for position in range(1, 9)
        )
        a_plans = tuple(
            placement.Plan(
                assignments=(("A", placement.Atom("inner", "修2库内", position)),),
                score=(0,),
            )
            for position in range(1, 7)
        )
        b_available = placement.SolveResult(
            plans=b_plans,
            explored_nodes=8,
            complete=True,
            budget_exhausted=False,
            frontier_truncated=False,
        )
        a_available = placement.SolveResult(
            plans=a_plans,
            explored_nodes=6,
            complete=True,
            budget_exhausted=False,
            frontier_truncated=False,
        )
        initial = solver.initial_state_without_pickup()
        attempted = Op("Get", "机走北", ("A",), (), ("A",))
        attempted_by_layout = {
            **{
                f"unified:B:{index:02d}": (attempted,) * (index + 1)
                for index in range(8)
            },
            "unified:B:00": (attempted, attempted, attempted),
            "unified:B:01": (attempted,),
            "unified:B:02": (attempted, attempted),
            **{
                f"unified:A:{index:02d}": (attempted,) * (4 + index)
                for index in range(6)
            },
        }
        calls: list[tuple[str, int]] = []

        def unified_placements(template: str) -> placement.SolveResult:
            solved = b_available if template == "B" else a_available
            solver.placement_candidates[template] = tuple(
                (
                    f"unified:{template}:{index:02d}",
                    plan,
                )
                for index, plan in enumerate(solved.plans)
            )
            return solved

        def solve_template(
            template: str,
            *,
            layout: str,
            transaction_expansion_limit: int,
        ) -> SearchResult:
            calls.append((layout, transaction_expansion_limit))
            committed_count = int(
                layout == "unified:B:01" and transaction_expansion_limit == 25
            )
            return SearchResult(
                status="partial",
                template=template,
                state=initial,
                ops=attempted_by_layout[layout],
                cost=(10**9, 0, 0, 0),
                reasons=("test_budget_exhausted",),
                expansions=transaction_expansion_limit,
                elapsed_seconds=0.0,
                layout=layout,
                committed_count=committed_count,
            )

        with (
            patch.object(
                solver,
                "unified_placements",
                side_effect=unified_placements,
            ),
            patch.object(
                solver,
                "build_assigned_line_by_no",
                return_value={"A": "修1库内"},
            ),
            patch.object(
                solver,
                "template_operation_lower_bound_components",
                side_effect=lambda template: {"test": 0 if template == "B" else 1},
            ),
            patch.object(solver, "solve_template", side_effect=solve_template),
            patch.object(solver, "validate_candidate", side_effect=lambda item: item),
            patch.object(
                solver,
                "result",
                side_effect=lambda chosen, results: {"chosen": chosen, "results": results},
            ),
        ):
            result = solver.solve()

        self.assertEqual(
            {
                layout
                for layout, limit in calls
                if limit == 25
            },
            {
                f"unified:B:{index:02d}"
                for index in range(8)
            },
        )
        self.assertEqual(
            [layout for layout, limit in calls if limit == 100],
            ["unified:B:00", "unified:B:01", "unified:B:02"],
        )
        calls_at_5_000 = [
            layout for layout, limit in calls if limit == 5_000
        ]
        calls_at_10_000 = [
            layout for layout, limit in calls if limit == 10_000
        ]
        self.assertEqual(len(calls_at_5_000), 3)
        self.assertEqual(len(calls_at_10_000), 3)
        self.assertEqual(
            set(calls_at_5_000),
            {"unified:B:00", "unified:B:01", "unified:B:02"},
        )
        self.assertEqual(result["chosen"].layout, "unified:B:01")
        self.assertEqual(result["chosen"].committed_count, 1)
        self.assertEqual(result["chosen"].ops, (attempted,))

    def test_restoration_line_keeps_operation_lower_bound_admissible(self) -> None:
        cases = {
            "outer_gate": (
                [
                    car("T", "机走北", 1, ["修1库内"]),
                    car("R", "修1库外", 1, ["存3线"]),
                ],
                1,
                4,
            ),
            "source_prefix": (
                [
                    car("R", "机走北", 1, ["存3线"]),
                    car("T", "机走北", 2, ["修1库内"]),
                ],
                0,
                2,
            ),
        }

        for name, (cars, expected_non_inner_puts, expected_lower_bound) in cases.items():
            with self.subTest(name=name):
                req = request(cars)
                solver = Stage3Solver(
                    name,
                    req,
                    EMPTY_STAGE2,
                    time_budget_seconds=5,
                )
                self.prepare_unified_assignment(solver)

                components = solver.template_operation_lower_bound_components("B")
                result = solver.solve()

                self.assertEqual(
                    components["non_inner_puts"],
                    expected_non_inner_puts,
                )
                self.assertEqual(sum(components.values()), expected_lower_bound)
                self.assertEqual(result["summary"]["status"], "complete")
                self.assertEqual(
                    result["summary"]["lower_bound_validation_violations"],
                    [],
                )
                self.assertLessEqual(
                    result["summary"]["operation_lower_bound"],
                    result["summary"]["business_hooks"],
                )
                self.assert_replay_clean(req, result)

    def test_put_preserves_all_possible_post_put_loco_positions(self) -> None:
        req = request([car("A", "机走北", 1, ["修1库内"])])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        state = State(lines=(), held=("A",), loco=("联7",), phase=1)

        with (
            patch.object(
                solver,
                "route",
                return_value=(("联7", "渡11", "卸轮线"), ""),
            ),
            patch.object(rv, "put_loco_positions", return_value={"联7", "渡11"}),
        ):
            _op, next_state, reject = solver.apply_put(state, "卸轮线", ("A",))

        self.assertEqual(reject, "")
        self.assertEqual(set(next_state.loco), {"联7", "渡11"})
        self.assertEqual(len(next_state.loco), 2)

    def test_inner_put_positions_match_assignment_and_replayed_terminal(self) -> None:
        req = request([
            car("A", "机走北", 1, ["修1库内"], forced=[1, 2, 3, 4, 5]),
        ])
        result = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5).solve()

        self.assertEqual(result["summary"]["status"], "complete")
        put = next(
            row
            for row in result["response"]["Data"]["Operations"]
            if row["Action"] == "Put"
        )
        planned = next(row for row in result["assignment_plan"] if row["no"] == "A")
        generated = next(
            row
            for row in result["response"]["Data"]["GeneratedEndStatus"]
            if row["No"] == "A"
        )

        self.assertIn(put["Positions"]["A"], {1, 2, 3, 4, 5})
        self.assertEqual(put["Positions"]["A"], planned["assigned_position"])
        self.assertEqual(
            (put["Line"], put["Positions"]["A"]),
            (generated["Line"], generated["Position"]),
        )
        self.assert_replay_clean(req, result)

    def test_single_forced_depot_position_is_preserved_as_a_sparse_slot(self) -> None:
        req = request([car("A", "机走北", 1, ["修4库内"], forced=[1])])

        result = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5).solve()
        put = next(
            row for row in result["response"]["Data"]["Operations"] if row["Action"] == "Put"
        )

        self.assertEqual(result["summary"]["status"], "complete")
        self.assertEqual(put["Positions"], {"A": 1})
        self.assert_replay_clean(req, result)

    def test_generated_status_uses_replayed_fixed_positions(self) -> None:
        req = request([
            car("A", "存4线", 1, ["修1库外"]),
            car("F1", "存4线", 2, ["存4线"]),
            car("F2", "存4线", 3, ["存4线"]),
        ])
        result = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5).solve()
        generated = {
            row["No"]: (row["Line"], row["Position"])
            for row in result["response"]["Data"]["GeneratedEndStatus"]
        }

        self.assertEqual(result["summary"]["status"], "complete")
        self.assertEqual(generated["F1"], ("存4线", 1))
        self.assertEqual(generated["F2"], ("存4线", 2))
        self.assert_replay_clean(req, result)

    def test_invalid_depot_stayer_is_repositioned(self) -> None:
        req = request([car("F", "修1库内", 1, ["修1库内"], process="厂修")])

        result = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5).solve()
        put = next(
            row for row in result["response"]["Data"]["Operations"] if row["Action"] == "Put"
        )

        self.assertEqual(result["summary"]["status"], "complete")
        self.assertEqual(result["summary"]["business_hooks"], 2)
        self.assertEqual(put["Positions"], {"F": 5})
        self.assert_replay_clean(req, result)

    def test_outer_assignment_does_not_displace_a_forced_stayer(self) -> None:
        req = request([
            car("F", "修1库外", 1, ["修1库外"], forced=[1]),
            car("A", "机走北", 1, ["修1库外", "修2库外"]),
        ])

        result = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5).solve()
        generated = {
            row["No"]: (row["Line"], row["Position"])
            for row in result["response"]["Data"]["GeneratedEndStatus"]
        }

        self.assertEqual(result["summary"]["status"], "complete")
        self.assertEqual(generated["F"], ("修1库外", 1))
        self.assertEqual(generated["A"][0], "修2库外")
        self.assert_replay_clean(req, result)

    def test_alternate_outer_target_remains_retrievable_until_assigned(self) -> None:
        req = request([
            car("A", "机走北", 1, ["修1库外", "修2库外"]),
        ])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        self.prepare_unified_assignment(solver)
        assigned = solver.assigned_line_by_no["A"]
        alternate = next(line for line in ("修1库外", "修2库外") if line != assigned)
        state = State(
            lines=((alternate, ("A",)),),
            held=(),
            loco=("联7",),
            phase=1,
            positioned_positions=(("A", 1),),
        )

        self.assertFalse(solver.outer_line_terminal_possible(state, alternate, ("A",)))
        self.assertIn((alternate, ("A",)), tuple(solver.get_prefixes(state)))

    def test_pending_weigh_is_owned_only_by_stage3_depot_tasks(self) -> None:
        cases = {
            "unwheel_support": (
                request([
                    car("A", "机走北", 1, ["修1库内"]),
                    car("F", "卸轮线", 1, ["卸轮线"], weigh=True),
                ]),
                "complete",
                2,
                ["A"],
            ),
            "outer_support": (
                request([
                    car("A", "机走北", 1, ["修1库内"]),
                    car("F", "修1库外", 1, ["修1库外"], weigh=True),
                ]),
                "complete",
                6,
                ["A"],
            ),
            "stage3_task": (
                request([car("A", "机走北", 1, ["修1库内"], weigh=True)]),
                "partial",
                0,
                ["A"],
            ),
            "stage4_deferred": (
                request([car("D", "卸轮线", 1, ["油漆线"], weigh=True)]),
                "complete",
                0,
                [],
            ),
        }

        for name, (req, status, hooks, business_nos) in cases.items():
            with self.subTest(name=name):
                result = Stage3Solver(
                    name,
                    req,
                    EMPTY_STAGE2,
                    time_budget_seconds=5,
                ).solve()
                summary = result["summary"]

                self.assertEqual(summary["status"], status)
                self.assertEqual(summary["business_hooks"], hooks)
                self.assertEqual(summary["stage3_business_nos"], business_nos)
                self.assertEqual(
                    rv.replay(result["stage3_request"], result["response"])[1],
                    [],
                )
                if name == "stage3_task":
                    self.assertIn("active_unweighed:A", summary["blocking_reasons"])
                else:
                    self.assertFalse(
                        any("weigh" in reason for reason in summary["blocking_reasons"])
                    )

    def test_outer_put_preserves_a_sparse_forced_position(self) -> None:
        req = request([car("A", "机走北", 1, ["修1库外"], forced=[3])])

        result = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5).solve()
        put = next(
            row for row in result["response"]["Data"]["Operations"] if row["Action"] == "Put"
        )

        self.assertEqual(result["summary"]["status"], "complete")
        self.assertEqual(put["Positions"], {"A": 3})
        self.assert_replay_clean(req, result)

    def test_unforced_outer_restoration_allows_compact_position_shift(self) -> None:
        req = request([
            car("A", "机走北", 1, ["修2库外"]),
            car("R", "修2库外", 1, ["修2库外"]),
        ])

        result = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5).solve()
        generated = {
            row["No"]: (row["Line"], row["Position"])
            for row in result["response"]["Data"]["GeneratedEndStatus"]
        }

        self.assertEqual(result["summary"]["status"], "complete")
        self.assertEqual(result["summary"]["business_hooks"], 2)
        self.assertEqual(generated["A"], ("修2库外", 1))
        self.assertEqual(generated["R"], ("修2库外", 2))
        self.assert_replay_clean(req, result)

    def test_source_prefix_support_is_restored_in_original_order(self) -> None:
        req = request([
            car("F1", "机走北", 1, ["机走北"], forced=[1]),
            car("F2", "机走北", 2, ["机走北"], forced=[2]),
            car("A", "机走北", 3, ["修1库内"]),
        ])

        result = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5).solve()
        generated = {
            row["No"]: (row["Line"], row["Position"])
            for row in result["response"]["Data"]["GeneratedEndStatus"]
        }

        self.assertEqual(result["summary"]["status"], "complete")
        self.assertEqual(generated["F1"], ("机走北", 1))
        self.assertEqual(generated["F2"], ("机走北", 2))
        self.assert_replay_clean(req, result)

    def test_depot_car_on_the_right_line_but_wrong_slot_is_repositioned(self) -> None:
        req = request([car("A", "修1库内", 1, ["修1库内"], forced=[2])])

        result = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5).solve()
        put = next(
            row for row in result["response"]["Data"]["Operations"] if row["Action"] == "Put"
        )

        self.assertEqual(result["summary"]["status"], "complete")
        self.assertEqual(result["summary"]["business_hooks"], 2)
        self.assertEqual(put["Positions"], {"A": 2})
        self.assert_replay_clean(req, result)

    def test_locked_section_stayer_is_preserved_by_unified_placement(self) -> None:
        req = request([
            car("S", "修1库内", 5, ["修1库内"], process="段修"),
            car(
                "F",
                "机走北",
                1,
                ["修1库内"],
                process="厂修",
                forced=[4, 5],
            ),
        ])

        result = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5).solve()
        generated = {
            row["No"]: (row["Line"], row["Position"])
            for row in result["response"]["Data"]["GeneratedEndStatus"]
        }

        self.assertEqual(result["summary"]["status"], "complete")
        self.assertEqual(generated["S"], ("修1库内", 5))
        self.assertEqual(generated["F"], ("修1库内", 4))
        self.assert_replay_clean(req, result)

    def test_solver_reports_only_unified_transaction_candidates(self) -> None:
        req = request([car("A", "机走北", 1, ["修1库内"], forced=[1])])

        result = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5).solve()
        summary = result["summary"]
        candidates = summary["template_summaries"]

        self.assertEqual(summary["status"], "complete")
        self.assertTrue(candidates)
        self.assertTrue(summary["layout"].startswith("unified:"))
        for candidate in candidates:
            self.assertTrue(candidate["layout"].startswith("unified:"))
            self.assertEqual(candidate["inner_clear_policy"], "transaction")
            self.assertFalse(candidate["deferred_clear"])

    def test_single_car_operation_lower_bound_is_two(self) -> None:
        req = request([car("A", "机走北", 1, ["修1库内"])])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        self.prepare_unified_assignment(solver)

        components = solver.template_operation_lower_bound_components("B")

        self.assertEqual(
            components,
            {
                "source_gets": 1,
                "inner_puts": 1,
                "non_inner_puts": 0,
                "shared_rehandle": 0,
            },
        )
        self.assertEqual(sum(components.values()), 2)

    def test_zero_lower_bound_and_incomplete_search_space_are_distinct(self) -> None:
        req = request([car("F", "存4线", 1, ["存4线"])])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        chosen = SearchResult(
            status="complete",
            template="none",
            state=solver.initial_state_without_pickup(),
            ops=(),
            cost=(0, 0, 0, 0),
            reasons=(),
            expansions=0,
            elapsed_seconds=0.0,
            lower_bound=0,
            lower_bound_scope="assignment_independent_relaxation",
        )
        not_evaluated = SearchResult(
            status="partial",
            template="unified",
            state=None,
            ops=(),
            cost=(10**9, 0, 0, 0),
            reasons=("stage3_global_time_budget_exhausted",),
            expansions=0,
            elapsed_seconds=0.0,
            layout="unified",
            inner_clear_policy="transaction",
            search_spec_evaluated=False,
        )

        summary = solver.result(chosen, [chosen, not_evaluated])["summary"]

        self.assertEqual(summary["operation_lower_bound"], 0)
        self.assertEqual(summary["operation_lower_bound_gap"], 0)
        self.assertEqual(
            summary["operation_lower_bound_scope"],
            "assignment_independent_relaxation",
        )
        self.assertFalse(summary["search_space_evaluation_complete"])
        self.assertEqual(summary["optimality_status"], "search_space_evaluation_incomplete")
        self.assertEqual(summary["template_summaries"][0]["operation_lower_bound_gap"], 0)

    def test_global_lower_bound_reached_proves_optimality_without_enumeration(self) -> None:
        req = request([car("A", "机走北", 1, ["修1库内"])])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        solver.search_space_evaluation_incomplete = True

        result = solver.solve()
        summary = result["summary"]

        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["evaluated_search_space_gap"], 0)
        self.assertFalse(summary["search_space_evaluation_complete"])
        self.assertEqual(
            summary["optimality_status"],
            "search_space_lower_bound_reached",
        )
        self.assertEqual(
            summary["elapsed_seconds"],
            summary["wall_elapsed_seconds"],
        )
        self.assertLessEqual(
            summary["chosen_search_seconds"],
            summary["wall_elapsed_seconds"],
        )

    def test_upstream_unavailable_summary_keeps_optimality_contract(self) -> None:
        summary = diagnostic_summary("TEST", "stage2_not_complete:partial")

        self.assertIsNone(summary["operation_lower_bound"])
        self.assertIsNone(summary["evaluated_search_space_lower_bound"])
        self.assertEqual(summary["operation_lower_bound_scope"], "not_applicable")
        self.assertEqual(summary["optimality_status"], "not_applicable")

    def test_template_a_counts_same_inner_line_in_two_epochs(self) -> None:
        req = request([
            car("A", "机走北", 1, ["修1库内"]),
            car("W", "洗油北", 1, ["修1库内"]),
        ])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        self.prepare_unified_assignment(solver, "A")

        components = solver.template_operation_lower_bound_components("A")

        self.assertEqual(components["inner_puts"], 2)
        self.assertEqual(components["source_gets"], 2)
        self.assertEqual(sum(components.values()), 4)

    def test_alignment_rehandle_is_shared_across_inner_lines(self) -> None:
        req = request([
            *[
                car(
                    f"S{index}",
                    "机走北",
                    index,
                    [f"修{index}库内"],
                    process="段修",
                    forced=[1],
                )
                for index in range(1, 4)
            ],
            *[
                car(
                    f"F{index}",
                    "洗油北",
                    index,
                    [f"修{index}库内"],
                    process="厂修",
                    forced=[4],
                )
                for index in range(1, 4)
            ],
        ])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        layout = self.prepare_unified_assignment(solver, "A")

        components = solver.template_operation_lower_bound_components("A")
        candidate = solver.solve_template(
            "A",
            layout=layout,
            transaction_expansion_limit=10_000,
        )

        self.assertEqual(components["source_gets"], 2)
        self.assertEqual(components["inner_puts"], 6)
        self.assertEqual(components["shared_rehandle"], 1)
        self.assertEqual(sum(components.values()), 9)
        self.assertEqual(candidate.status, "complete")
        self.assertEqual(solver.business_hook_count(candidate.ops), 10)
        self.assertEqual(candidate.lower_bound, 9)
        self.assertLessEqual(
            candidate.lower_bound,
            solver.business_hook_count(candidate.ops),
        )

    def test_long_non_inner_load_requires_multiple_staging_puts(self) -> None:
        req = request([
            car(str(index), "修1库内", index, ["油漆线"])
            for index in range(1, 5)
        ])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        self.prepare_unified_assignment(solver)

        components = solver.template_operation_lower_bound_components("B")

        self.assertEqual(components["non_inner_puts"], 2)
        self.assertGreater(
            sum(float(solver.meta[str(index)]["Length"]) for index in range(1, 5)),
            max(
                float(rv.TRACK_LEN[line])
                for line in (
                    "卸轮线",
                    "修1库外",
                    "修2库外",
                    "修3库外",
                    "修4库外",
                )
            ),
        )

    def test_complete_candidates_never_beat_their_operation_lower_bound(self) -> None:
        cases = {
            "single_inner": [car("A", "机走北", 1, ["修1库内"])],
            "two_sources": [
                car("A", "机走北", 1, ["修1库内"]),
                car("B", "洗油北", 1, ["修2库内"]),
            ],
            "mixed_terminal": [
                car("A", "机走北", 1, ["修1库内"], forced=[1]),
                car("B", "机走北", 2, ["修2库外"], forced=[1]),
            ],
        }

        completed_candidates = 0
        for case_id, cars in cases.items():
            with self.subTest(case_id=case_id):
                req = request(cars)
                result = Stage3Solver(
                    case_id,
                    req,
                    EMPTY_STAGE2,
                    time_budget_seconds=5,
                ).solve()

                self.assertEqual(result["summary"]["status"], "complete")
                self.assertEqual(
                    result["summary"]["lower_bound_validation_violations"],
                    [],
                )
                self.assert_replay_clean(req, result)
                for candidate in result["summary"]["template_summaries"]:
                    if candidate["status"] != "complete":
                        continue
                    completed_candidates += 1
                    self.assertLessEqual(
                        candidate["operation_lower_bound"],
                        candidate["operations"],
                    )

        self.assertGreater(completed_candidates, 0)


if __name__ == "__main__":
    unittest.main()
