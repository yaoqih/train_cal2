from __future__ import annotations

import json
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

import replay_validator as rv
from stage3_simple.solve import (
    TEMPLATE_B_ORDER,
    Op,
    SearchResult,
    Stage3Solver,
    State,
    case_id_from_path,
    unavailable_summary,
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
    def test_invalid_stage2_combined_response_is_explicitly_rejected(self) -> None:
        req = request([
            car("A", "机走北", 1, ["修1库内"]),
        ])
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

        with self.assertRaisesRegex(ValueError, "terminal_inspection_mode_missing_or_invalid"):
            Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)

    def test_complete_summary_requires_combined_replay_to_be_clean(self) -> None:
        req = request([
            car("F", "存4线", 1, ["存4线"]),
        ])
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
        valid = solver.validate_candidate(
            solver.solve_template(
                "B",
                layout="cohesive",
                deferred_clear=True,
                terminal_merge=True,
                inner_clear_policy="eager",
                allow_search=False,
            )
        )
        self.assertEqual(valid.status, "complete")
        assert valid.state is not None
        replayed_position = dict(valid.state.positioned_positions)["A"]
        different_legal_position = 4 if replayed_position != 4 else 3
        invalid_state = replace(
            valid.state,
            positioned_positions=(("A", different_legal_position),),
        )
        invalid_cheap = replace(
            valid,
            state=invalid_state,
            cost=(0, 0, 0, 0),
        )

        rejected = solver.validate_candidate(invalid_cheap)
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
            patch.object(solver, "blocking_inner_get_prefixes", return_value=[("修1库内", ("B",))]),
            patch.object(solver, "apply_get", return_value=(get_op, after_get, "")),
            patch.object(solver, "put_suffixes", return_value=[("修1库外", ("H",))]),
            patch.object(solver, "apply_put", return_value=(put_op, after_put, "")),
            patch.object(solver, "get_prefixes", return_value=[]),
        ):
            neighbors = list(solver.neighbors(state, "B"))

        legal_actions = {op.action for op, _next_state, reject in neighbors if not reject}
        self.assertEqual(legal_actions, {"Get", "Put"})

    def test_gate_clear_macro_has_only_bounded_atomic_shapes(self) -> None:
        initial = State(lines=(), held=("A",), loco=("联7",), phase=1)
        after_get = State(lines=(), held=("A", "G", "T"), loco=("修1库外",), phase=1)
        after_gate = State(lines=(), held=("A",), loco=("修2库内",), phase=1)
        after_terminal = State(lines=(), held=("A", "G"), loco=("卸轮线",), phase=1)
        after_gate_prefix = State(lines=(), held=("A",), loco=("修3库内",), phase=1)
        after_relocate = State(lines=(), held=("A",), loco=("卸轮线",), phase=1)
        final = State(lines=(), held=(), loco=("修1库内",), phase=1)
        get_gate = Op("Get", "修1库外", ("G", "T"), (), ("A", "G", "T"))
        put_gate = Op("Put", "修2库内", ("G", "T"), (), ("A",))
        put_terminal = Op("Put", "卸轮线", ("T",), (), ("A", "G"))
        put_gate_prefix = Op("Put", "修3库内", ("G",), (), ("A",))
        put_relocate = Op("Put", "卸轮线", ("G", "T"), (), ("A",))
        put_original = Op("Put", "修1库内", ("A",), (), ())
        scenarios = {
            "permanent": {
                "ready": [(put_gate, after_gate), (put_original, final)],
                "terminal": AssertionError("terminal branch must not run"),
                "relocate": AssertionError("relocation branch must not run"),
                "lines": ("修1库外", "修2库内", "修1库内"),
            },
            "terminal": {
                "ready": [None, (put_gate_prefix, after_gate_prefix), (put_original, final)],
                "terminal": (put_terminal, after_terminal),
                "relocate": AssertionError("relocation branch must not run"),
                "lines": ("修1库外", "卸轮线", "修3库内", "修1库内"),
            },
            "relocate": {
                "ready": [None, (put_original, final)],
                "terminal": None,
                "relocate": (put_relocate, after_relocate),
                "lines": ("修1库外", "卸轮线", "修1库内"),
            },
        }

        def return_or_raise(value: object):
            def effect(*_args: object, **_kwargs: object):
                if isinstance(value, BaseException):
                    raise value
                return value

            return effect

        for name, scenario in scenarios.items():
            with self.subTest(name=name):
                solver = object.__new__(Stage3Solver)
                with (
                    patch.object(solver, "common_assigned_target", return_value="修1库内"),
                    patch.object(solver, "depot_move_is_ready", return_value=True),
                    patch.object(solver, "plan_depot_put_positions", return_value=({}, "")),
                    patch.object(solver, "line_map", return_value={"修1库外": ("G", "T")}),
                    patch.object(
                        solver,
                        "apply_get",
                        return_value=(get_gate, after_get, ""),
                    ) as apply_get,
                    patch.object(
                        solver,
                        "greedy_put_ready_inner",
                        side_effect=scenario["ready"],
                    ),
                    patch.object(
                        solver,
                        "greedy_put_exposed_terminal_suffix",
                        side_effect=return_or_raise(scenario["terminal"]),
                    ),
                    patch.object(
                        solver,
                        "greedy_put_buffer_block",
                        side_effect=return_or_raise(scenario["relocate"]),
                    ),
                ):
                    result = solver.greedy_clear_gate_macro(initial)

                self.assertIsNotNone(result)
                assert result is not None
                macro_ops, result_state = result
                self.assertEqual(tuple(op.line for op in macro_ops), scenario["lines"])
                self.assertEqual(result_state, final)
                apply_get.assert_called_once_with(initial, "修1库外", ("G", "T"))

    def test_greedy_finish_commits_gate_macro_without_replaying_its_first_get(self) -> None:
        initial = State(lines=(), held=("A",), loco=("联7",), phase=1)
        final = State(lines=(), held=(), loco=("修1库内",), phase=1)
        initial_op = Op("Get", "机走北", ("A",), (), ("A",))
        macro_ops = (
            Op("Get", "修1库外", ("G",), (), ("A", "G")),
            Op("Put", "卸轮线", ("G",), (), ("A",)),
            Op("Put", "修1库内", ("A",), (), ()),
        )

        for policy in ("eager", "just_in_time"):
            with self.subTest(policy=policy):
                solver = object.__new__(Stage3Solver)
                with (
                    patch.object(solver, "deadline_reached", return_value=False),
                    patch.object(solver, "complete", side_effect=[False, True]),
                    patch.object(solver, "greedy_get_blocking_inner", return_value=None),
                    patch.object(
                        solver,
                        "greedy_clear_gate_macro",
                        return_value=(macro_ops, final),
                    ),
                    patch.object(
                        solver,
                        "apply_get",
                        side_effect=AssertionError("macro Get was replayed"),
                    ),
                    patch.object(solver, "ops_cost", return_value=(4, 0, 0, 0)),
                ):
                    result = solver.greedy_finish(
                        "B",
                        initial,
                        [initial_op],
                        0.0,
                        terminal_merge=True,
                        inner_clear_policy=policy,
                    )

                self.assertEqual(result.status, "complete")
                self.assertEqual(result.state, final)
                self.assertEqual(result.ops, (initial_op, *macro_ops))

    def test_proved_nested_gate_failure_never_commits_debt_relocation(self) -> None:
        solver = object.__new__(Stage3Solver)
        initial = State(lines=(), held=("A",), loco=("联7",), phase=1)
        after_get = State(lines=(), held=("A", "G"), loco=("修1库外",), phase=1)
        after_relocate = State(lines=(), held=("A",), loco=("修4库外",), phase=1)
        get_gate = Op("Get", "修1库外", ("G",), (), ("A", "G"))
        debt_relocation = Op("Put", "修4库外", ("G",), (), ("A",))
        dependency = ("修2库内", "修2库外", ("X",))

        with (
            patch.object(solver, "common_assigned_target", return_value="修1库内"),
            patch.object(solver, "depot_move_is_ready", return_value=True),
            patch.object(solver, "plan_depot_put_positions", return_value=({}, "")),
            patch.object(solver, "line_map", return_value={"修1库外": ("G",)}),
            patch.object(
                solver,
                "apply_get",
                return_value=(get_gate, after_get, ""),
            ),
            patch.object(solver, "greedy_put_ready_inner", return_value=None),
            patch.object(solver, "greedy_put_exposed_terminal_suffix", return_value=None),
            patch.object(
                solver,
                "greedy_put_buffer_block",
                return_value=(debt_relocation, after_relocate),
            ),
            patch.object(solver, "pending_inner_targets", return_value={"修4库内"}),
            patch.object(
                solver,
                "nested_gate_dependency",
                return_value=dependency,
            ),
            patch.object(
                solver,
                "greedy_nested_gate_swap",
                return_value=None,
            ) as nested_swap,
        ):
            result = solver.greedy_clear_gate_macro(initial)

        self.assertIsNone(result)
        nested_swap.assert_called_once_with(
            after_get,
            original_target="修1库内",
            original_move=("A",),
            original_outer="修1库外",
            first_gate=("G",),
            dependency=dependency,
        )

    def test_partial_response_drops_open_train_operation_prefix(self) -> None:
        req = request([
            car("A", "机走北", 1, ["修1库内"]),
        ])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
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
            layout="cost",
        )

        result = solver.result(chosen, [chosen])
        operations = result["response"]["Data"]["Operations"]
        _replayed, violations = rv.replay(result["stage3_request"], result["response"])

        self.assertEqual(operations, [])
        self.assertFalse(
            any(item.code == "dirty_train_after_last_operation" for item in violations)
        )

    def test_closed_partial_response_exposes_no_operations_or_generated_state(self) -> None:
        req = request([car("A", "机走北", 1, ["修1库内"])])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        attempted = Op("Get", "机走北", ("A",), (), ("A",))
        chosen = SearchResult(
            status="partial",
            template="B",
            state=solver.initial_state_without_pickup(),
            ops=(attempted,),
            cost=(10**9, 0, 0, 0),
            reasons=("greedy_no_completion",),
            expansions=0,
            elapsed_seconds=0.0,
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

    def test_put_preserves_all_possible_post_put_loco_positions(self) -> None:
        req = request([
            car("A", "机走北", 1, ["修1库内"]),
        ])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        state = State(lines=(), held=("A",), loco=("联7",), phase=1)

        with (
            patch.object(solver, "route", return_value=(("联7", "渡11", "卸轮线"), "")),
            patch.object(rv, "put_loco_positions", return_value={"联7", "渡11"}),
        ):
            _op, next_state, reject = solver.apply_put(state, "卸轮线", ("A",))

        self.assertEqual(reject, "")
        self.assertEqual(set(next_state.loco), {"联7", "渡11"})
        self.assertEqual(len(next_state.loco), 2)

    def test_inner_put_emits_exact_positions_and_clean_replay(self) -> None:
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
        self.assertEqual(put["Positions"], {"A": 5})
        self.assertEqual(rv.replay(result["stage3_request"], result["response"])[1], [])
        self.assertEqual(rv.replay(req, result["combined_response"])[1], [])

    def test_single_forced_depot_position_is_preserved_as_a_sparse_slot(self) -> None:
        req = request([
            car("A", "机走北", 1, ["修4库内"], forced=[1]),
        ])

        result = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5).solve()
        put = next(
            row for row in result["response"]["Data"]["Operations"]
            if row["Action"] == "Put"
        )

        self.assertEqual(result["summary"]["status"], "complete")
        self.assertEqual(put["Positions"], {"A": 1})
        self.assertEqual(rv.replay(result["stage3_request"], result["response"])[1], [])

    def test_cost_layout_is_a_global_minimum(self) -> None:
        req = request([
            car("A", "机南", 1, ["修3库内"], forced=[4, 5]),
            car(
                "B",
                "机走北",
                1,
                ["修1库内", "修2库内", "修3库内", "修4库内"],
                length=17.6,
                forced=[4],
            ),
        ])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        solver.build_assigned_line_by_no("B", "cost")

        self.assertEqual(
            solver.assigned_slot_by_no,
            {"A": ("修3库内", 5), "B": ("修3库内", 4)},
        )
        total_cost = sum(
            solver.slot_preference_cost(no, slot, "B")
            for no, slot in solver.assigned_slot_by_no.items()
        )
        self.assertEqual(total_cost, 7)

    def test_alignment_frontier_uses_layout_slots_without_forced_positions(self) -> None:
        req = request([
            car("A", "机走北", 1, ["修1库内"]),
            car("B", "机走北", 2, ["修1库内"]),
        ])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        solver.assigned_line_by_no = solver.build_assigned_line_by_no("B", "cohesive")
        state = State(lines=(), held=("A", "B"), loco=("联7",), phase=1)
        deepest = max(
            (slot[1], no)
            for no, slot in solver.assigned_slot_by_no.items()
            if slot[0] == "修1库内"
        )[1]

        self.assertEqual(solver.next_assigned_no_by_line(state)["修1库内"], deepest)

    def test_all_strategies_are_compared_and_template_a_can_win(self) -> None:
        req = request([
            car("A", "机走北", 1, ["修4库内"], forced=[1, 2, 3, 4, 5]),
            car("B", "机南", 1, ["修4库内"], forced=[1, 2, 3, 4, 5]),
            car(
                "C",
                "洗油北",
                1,
                ["修1库内", "修2库内", "修3库内"],
                forced=[1, 2, 3, 4, 5],
            ),
        ])
        result = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5).solve()
        summary = result["summary"]

        self.assertEqual(summary["template"], "A")
        self.assertEqual(summary["layout"], "cohesive")
        self.assertEqual(summary["operations"], 5)
        self.assertEqual(
            {(row["template"], row["layout"]) for row in summary["template_summaries"]},
            {("A", "cohesive"), ("B", "cohesive"), ("A", "cost"), ("B", "cost")},
        )
        self.assertEqual(rv.replay(result["stage3_request"], result["response"])[1], [])

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
        self.assertEqual(rv.replay(result["stage3_request"], result["response"])[1], [])

    def test_invalid_depot_stayer_is_repositioned(self) -> None:
        req = request([
            car("F", "修1库内", 1, ["修1库内"], process="厂修"),
        ])

        result = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5).solve()
        put = next(
            row
            for row in result["response"]["Data"]["Operations"]
            if row["Action"] == "Put"
        )

        self.assertEqual(result["summary"]["status"], "complete")
        self.assertEqual(result["summary"]["business_hooks"], 2)
        self.assertEqual(put["Positions"], {"F": 5})
        self.assertEqual(rv.replay(result["stage3_request"], result["response"])[1], [])
        self.assertEqual(rv.replay(req, result["combined_response"])[1], [])

    def test_outer_prefix_stayer_is_moved_and_restored_with_the_task_car(self) -> None:
        req = request([
            car("F", "修1库外", 1, ["修1库外"]),
            car("A", "修1库外", 2, ["修2库外"]),
        ])

        result = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5).solve()
        summary = result["summary"]
        operations = result["response"]["Data"]["Operations"]

        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["business_hooks"], 3)
        self.assertEqual(operations[0]["Action"], "Get")
        self.assertEqual(operations[0]["MoveCars"], ["F", "A"])
        self.assertEqual(rv.replay(result["stage3_request"], result["response"])[1], [])
        self.assertEqual(rv.replay(req, result["combined_response"])[1], [])

    def test_outer_assignment_avoids_displacing_a_forced_stayer(self) -> None:
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
        self.assertEqual(result["summary"]["business_hooks"], 2)
        self.assertEqual(generated["F"], ("修1库外", 1))
        self.assertEqual(generated["A"][0], "修2库外")
        self.assertEqual(rv.replay(result["stage3_request"], result["response"])[1], [])
        self.assertEqual(rv.replay(req, result["combined_response"])[1], [])

    def test_forced_outer_stayer_is_parked_then_restored_around_unique_target(self) -> None:
        req = request([
            car("F", "修1库外", 1, ["修1库外"], forced=[1]),
            car("A", "机走北", 1, ["修1库外"], forced=[2]),
        ])

        result = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5).solve()
        summary = result["summary"]
        operations = result["response"]["Data"]["Operations"]

        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["business_hooks"], 6)
        self.assertEqual(summary["task_nos"], ["A"])
        self.assertEqual(summary["restoration_nos"], ["F"])
        self.assertEqual(
            [(row["Action"], row["Line"], row["MoveCars"]) for row in operations],
            [
                ("Get", "机走北", ["A"]),
                ("Get", "修1库外", ["F"]),
                ("Put", "卸轮线", ["F"]),
                ("Put", "修1库外", ["A"]),
                ("Get", "卸轮线", ["F"]),
                ("Put", "修1库外", ["F"]),
            ],
        )
        self.assertEqual(rv.replay(result["stage3_request"], result["response"])[1], [])
        self.assertEqual(rv.replay(req, result["combined_response"])[1], [])

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
        req = request([
            car("A", "机走北", 1, ["修1库外"], forced=[3]),
        ])

        result = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5).solve()
        put = next(
            row
            for row in result["response"]["Data"]["Operations"]
            if row["Action"] == "Put"
        )

        self.assertEqual(result["summary"]["status"], "complete")
        self.assertEqual(put["Positions"], {"A": 3})
        self.assertEqual(rv.replay(result["stage3_request"], result["response"])[1], [])
        self.assertEqual(rv.replay(req, result["combined_response"])[1], [])

    def test_outer_compact_put_projects_existing_positions(self) -> None:
        req = request([
            car("F", "修1库外", 1, ["修1库外"]),
            car("A", "机走北", 1, ["修1库外"]),
        ])

        result = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5).solve()
        generated = {
            row["No"]: (row["Line"], row["Position"])
            for row in result["response"]["Data"]["GeneratedEndStatus"]
        }
        put = next(
            row
            for row in result["response"]["Data"]["Operations"]
            if row["Action"] == "Put"
        )

        self.assertEqual(result["summary"]["status"], "complete")
        self.assertEqual(result["summary"]["business_hooks"], 2)
        self.assertNotIn("Positions", put)
        self.assertEqual(generated["A"], ("修1库外", 1))
        self.assertEqual(generated["F"], ("修1库外", 2))
        self.assertEqual(rv.replay(result["stage3_request"], result["response"])[1], [])
        self.assertEqual(rv.replay(req, result["combined_response"])[1], [])

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
        self.assertEqual(result["summary"]["business_hooks"], 3)
        self.assertEqual(generated["F1"], ("机走北", 1))
        self.assertEqual(generated["F2"], ("机走北", 2))
        self.assertEqual(rv.replay(result["stage3_request"], result["response"])[1], [])
        self.assertEqual(rv.replay(req, result["combined_response"])[1], [])

    def test_depot_car_on_the_right_line_but_wrong_slot_is_repositioned(self) -> None:
        req = request([
            car("A", "修1库内", 1, ["修1库内"], forced=[2]),
        ])

        result = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5).solve()
        put = next(
            row
            for row in result["response"]["Data"]["Operations"]
            if row["Action"] == "Put"
        )

        self.assertEqual(result["summary"]["status"], "complete")
        self.assertEqual(result["summary"]["business_hooks"], 2)
        self.assertEqual(put["Positions"], {"A": 2})
        self.assertEqual(rv.replay(result["stage3_request"], result["response"])[1], [])
        self.assertEqual(rv.replay(req, result["combined_response"])[1], [])

    def test_locked_section_stayer_is_exempt_from_new_factory_order(self) -> None:
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

        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        solver.assigned_line_by_no = solver.build_assigned_line_by_no("B", "cohesive")
        built = solver.apply_pickup_template(TEMPLATE_B_ORDER, phase=1, template="B")
        assert isinstance(built, tuple)
        state, _ops = built
        _put, final_state, reject = solver.apply_put(state, "修1库内", ("F",))
        terminal_ok, reasons, _positions = solver.terminal_depot_ok(final_state)

        self.assertEqual(reject, "")
        self.assertTrue(terminal_ok)
        self.assertFalse(any("depot_section_after_factory:S" in reason for reason in reasons))

    def test_single_car_operation_lower_bound_is_two(self) -> None:
        req = request([
            car("A", "机走北", 1, ["修1库内"]),
        ])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        solver.build_assigned_line_by_no("B", "cohesive")

        components = solver.template_operation_lower_bound_components("B")

        self.assertEqual(
            components,
            {
                "source_gets": 1,
                "inner_puts": 1,
                "non_inner_puts": 0,
                "frontier_rehandle": 0,
            },
        )
        self.assertEqual(solver.template_operation_lower_bound("B"), 2)

    def test_zero_lower_bound_and_incomplete_portfolio_are_distinct(self) -> None:
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
            template="B",
            state=None,
            ops=(),
            cost=(10**9, 0, 0, 0),
            reasons=("stage3_global_time_budget_exhausted",),
            expansions=0,
            elapsed_seconds=0.0,
            strategy_evaluated=False,
        )

        summary = solver.result(chosen, [chosen, not_evaluated])["summary"]

        self.assertEqual(summary["operation_lower_bound"], 0)
        self.assertEqual(summary["operation_lower_bound_gap"], 0)
        self.assertEqual(
            summary["operation_lower_bound_scope"],
            "assignment_independent_relaxation",
        )
        self.assertFalse(summary["portfolio_evaluation_complete"])
        self.assertEqual(summary["optimality_status"], "portfolio_evaluation_incomplete")
        self.assertEqual(summary["template_summaries"][0]["operation_lower_bound_gap"], 0)

    def test_upstream_unavailable_summary_keeps_optimality_contract(self) -> None:
        summary = unavailable_summary("TEST", "stage2_not_complete:partial")

        self.assertIsNone(summary["operation_lower_bound"])
        self.assertIsNone(summary["evaluated_strategy_portfolio_lower_bound"])
        self.assertEqual(summary["operation_lower_bound_scope"], "not_applicable")
        self.assertEqual(summary["optimality_status"], "not_applicable")

    def test_template_a_counts_same_inner_line_in_two_epochs(self) -> None:
        req = request([
            car("A", "机走北", 1, ["修1库内"]),
            car("W", "洗油北", 1, ["修1库内"]),
        ])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        solver.build_assigned_line_by_no("A", "cohesive")

        components = solver.template_operation_lower_bound_components("A")

        self.assertEqual(components["inner_puts"], 2)
        self.assertEqual(components["source_gets"], 2)
        self.assertEqual(sum(components.values()), 4)

    def test_template_a_frontier_debt_excludes_inner_lines_completed_in_prior_epoch(self) -> None:
        req = request([
            car("I", "机走北", 1, ["修1库内"]),
            car("O", "洗油北", 1, ["修1库外"]),
        ])

        result = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5).solve()
        summary = result["summary"]

        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["template"], "A")
        self.assertEqual(summary["operations"], 4)
        self.assertEqual(summary["operation_lower_bound"], 4)
        self.assertEqual(summary["operation_lower_bound_gap"], 0)
        self.assertEqual(summary["lower_bound_validation_violations"], [])

    def test_non_inner_length_over_one_staging_line_requires_two_puts(self) -> None:
        req = request([
            car(str(index), "修1库内", index, ["油漆线"])
            for index in range(1, 5)
        ])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        solver.build_assigned_line_by_no("B", "cost")

        components = solver.template_operation_lower_bound_components("B")

        self.assertEqual(components["non_inner_puts"], 2)
        self.assertGreater(
            sum(float(solver.meta[str(index)]["Length"]) for index in range(1, 5)),
            max(float(rv.TRACK_LEN[line]) for line in ("卸轮线", "修1库外", "修2库外", "修3库外", "修4库外")),
        )

    def test_0117z_operation_lower_bound_components_are_exact(self) -> None:
        stage2_dir = ROOT / "artifacts" / "four_stage_balanced_current" / "stage2"
        stage2_path = stage2_dir / "0117Z_combined_response.json"
        if not stage2_path.exists():
            self.skipTest("local 0117Z stage2 regression artifact is not available")
        request_path = next((ROOT / "data" / "truth2").glob("*0117Z.json"))
        req = json.loads(request_path.read_text(encoding="utf-8-sig"))
        stage2 = json.loads(stage2_path.read_text(encoding="utf-8"))

        result = Stage3Solver("0117Z", req, stage2, time_budget_seconds=5).solve()
        components = result["summary"]["operation_lower_bound_components"]

        self.assertEqual(
            components,
            {
                "source_gets": 6,
                "inner_puts": 5,
                "non_inner_puts": 2,
                "frontier_rehandle": 1,
            },
        )
        self.assertEqual(result["summary"]["operation_lower_bound"], 14)

    def test_all_complete_full_corpus_candidates_respect_operation_lower_bound(self) -> None:
        stage2_dir = ROOT / "artifacts" / "four_stage_balanced_current" / "stage2"
        if not stage2_dir.exists():
            self.skipTest("local full stage2 regression artifacts are not available")

        completed_candidates = 0
        violations: list[tuple[str, str, str, int, int]] = []
        for request_path in sorted((ROOT / "data" / "truth2").glob("validation_*.json")):
            case_id = case_id_from_path(request_path)
            stage2_path = stage2_dir / f"{case_id}_combined_response.json"
            stage2_summary_path = stage2_dir / f"{case_id}_summary.json"
            if not stage2_path.exists():
                continue
            if (
                stage2_summary_path.exists()
                and json.loads(stage2_summary_path.read_text(encoding="utf-8")).get("status") != "complete"
            ):
                continue
            req = json.loads(request_path.read_text(encoding="utf-8-sig"))
            stage2 = json.loads(stage2_path.read_text(encoding="utf-8"))
            result = Stage3Solver(case_id, req, stage2, time_budget_seconds=5).solve()
            for candidate in result["summary"].get("template_summaries", []):
                if candidate.get("status") != "complete":
                    continue
                completed_candidates += 1
                lower_bound = int(candidate.get("operation_lower_bound") or 0)
                hooks = int(candidate.get("operations") or 0)
                if lower_bound > hooks:
                    violations.append(
                        (
                            case_id,
                            str(candidate.get("template") or ""),
                            str(candidate.get("layout") or ""),
                            lower_bound,
                            hooks,
                        )
                    )

        self.assertGreater(completed_candidates, 0)
        self.assertEqual(violations, [])

    def test_deferred_clear_physical_failure_is_explicit_partial(self) -> None:
        req = request([
            car("A", "机走北", 1, ["修1库内"]),
            car("D", "修1库外", 1, ["油漆线"]),
        ])
        solver = Stage3Solver("TEST", req, EMPTY_STAGE2, time_budget_seconds=5)
        original_apply_put = solver.apply_put

        def fail_deferred_put(state: State, line: str, move: tuple[str, ...]):
            if line == "卸轮线":
                return Op("Put", line, move, (), state.held), state, "forced_physical_failure"
            return original_apply_put(state, line, move)

        with patch.object(solver, "apply_put", side_effect=fail_deferred_put):
            built = solver.apply_pickup_template(
                TEMPLATE_B_ORDER,
                phase=1,
                template="B",
                deferred_clear=True,
            )

        self.assertIsInstance(built, SearchResult)
        assert isinstance(built, SearchResult)
        self.assertEqual(built.status, "partial")
        self.assertEqual(built.reasons, ("deferred_clear_forced_physical_failure",))
        self.assertIsNone(built.state)

    def test_real_difficult_cases_are_solved_without_search(self) -> None:
        stage2_dir = ROOT / "artifacts" / "four_stage_balanced_current" / "stage2"
        cases = {
            "0104Z": (9, 0),
            "0112Z": (14, None),
            "0117Z": (14, 0),
            "0226W": (10, 0),
        }
        if not all((stage2_dir / f"{case_id}_combined_response.json").exists() for case_id in cases):
            self.skipTest("local stage2 regression artifacts are not available")

        for case_id, (hook_limit, expected_gap) in cases.items():
            with self.subTest(case_id=case_id):
                request_path = next((ROOT / "data" / "truth2").glob(f"*{case_id}.json"))
                req = json.loads(request_path.read_text(encoding="utf-8-sig"))
                stage2 = json.loads(
                    (stage2_dir / f"{case_id}_combined_response.json").read_text(encoding="utf-8")
                )
                result = Stage3Solver(case_id, req, stage2, time_budget_seconds=5).solve()
                summary = result["summary"]

                self.assertEqual(summary["status"], "complete")
                self.assertLessEqual(summary["operations"], hook_limit)
                if expected_gap is not None:
                    self.assertEqual(summary["evaluated_strategy_portfolio_gap"], expected_gap)
                    self.assertEqual(summary["optimality_status"], "portfolio_lower_bound_reached")
                self.assertEqual(summary["expansions"], 0)
                self.assertEqual(rv.replay(result["stage3_request"], result["response"])[1], [])
                self.assertEqual(rv.replay(req, result["combined_response"])[1], [])

    def test_truth3_exact_alignment_cases_complete_without_exact_search(self) -> None:
        stage2_dir = ROOT / "artifacts" / "stage1-4_simple" / "truth3" / "stage2"
        cases = {
            "0401Z": 12,
            "0408W": 17,
            "0416W": 33,
            "0420W": 28,
            "0427W": 27,
            "0429Z": 16,
        }
        if not all((stage2_dir / f"{case_id}_combined_response.json").exists() for case_id in cases):
            self.skipTest("local truth3 stage2 regression artifacts are not available")

        for case_id, hook_limit in cases.items():
            with self.subTest(case_id=case_id):
                request_path = next((ROOT / "data" / "truth3").glob(f"*{case_id}.json"))
                req = json.loads(request_path.read_text(encoding="utf-8-sig"))
                stage2 = json.loads(
                    (stage2_dir / f"{case_id}_combined_response.json").read_text(encoding="utf-8")
                )
                result = Stage3Solver(case_id, req, stage2, time_budget_seconds=5).solve()
                summary = result["summary"]

                self.assertEqual(summary["status"], "complete")
                self.assertLessEqual(summary["operations"], hook_limit)
                self.assertNotEqual(summary["inner_clear_policy"], "exact")
                self.assertEqual(summary["expansions"], 0)
                self.assertEqual(rv.replay(result["stage3_request"], result["response"])[1], [])
                self.assertEqual(rv.replay(req, result["combined_response"])[1], [])
                if case_id == "0420W":
                    notes = {row["note"] for row in result["trace"]}
                    self.assertTrue(
                        any(note.startswith("atomic_nested_gate_swap_out:") for note in notes)
                    )
                    self.assertTrue(
                        any(note.startswith("atomic_nested_gate_restore:") for note in notes)
                    )

    def test_0420w_sparse_factory_slots_use_exact_matching(self) -> None:
        stage2_dir = ROOT / "artifacts" / "stage1-4_simple" / "truth3" / "stage2"
        stage2_path = stage2_dir / "0420W_combined_response.json"
        if not stage2_path.exists():
            self.skipTest("local truth3 stage2 regression artifact is not available")
        request_path = next((ROOT / "data" / "truth3").glob("*0420W.json"))
        req = json.loads(request_path.read_text(encoding="utf-8-sig"))
        stage2 = json.loads(stage2_path.read_text(encoding="utf-8"))
        solver = Stage3Solver("0420W", req, stage2, time_budget_seconds=5)

        solver.build_assigned_line_by_no("A", "cohesive")
        self.assertEqual(
            solver.assignment_reasons,
            ("cohesive_direct_unload_order_infeasible:A:5310676",),
        )
        solver.build_assigned_line_by_no("A", "cost")

        self.assertEqual(solver.assignment_reasons, ())
        self.assertEqual(solver.assigned_slot_by_no["5310676"], ("修1库内", 4))
        self.assertEqual(solver.assigned_slot_by_no["5317385"], ("修1库内", 5))

    def test_truth3_infeasible_cases_emit_capacity_certificates(self) -> None:
        stage2_dir = ROOT / "artifacts" / "stage1-4_simple" / "truth3" / "stage2"
        expected = {
            "0406W": "inner_slot_capacity_infeasible:cars=14>reachable_slots=13",
            "0424Z": "outer_capacity_infeasible:修3库外:demand=52.8>capacity=49.3",
        }
        if not all((stage2_dir / f"{case_id}_combined_response.json").exists() for case_id in expected):
            self.skipTest("local truth3 stage2 regression artifacts are not available")

        for case_id, certificate_prefix in expected.items():
            with self.subTest(case_id=case_id):
                request_path = next((ROOT / "data" / "truth3").glob(f"*{case_id}.json"))
                req = json.loads(request_path.read_text(encoding="utf-8-sig"))
                stage2 = json.loads(
                    (stage2_dir / f"{case_id}_combined_response.json").read_text(encoding="utf-8")
                )
                summary = Stage3Solver(
                    case_id,
                    req,
                    stage2,
                    time_budget_seconds=5,
                ).solve()["summary"]

                self.assertEqual(summary["status"], "partial")
                self.assertTrue(
                    any(
                        certificate.startswith(certificate_prefix)
                        for certificate in summary["infeasibility_certificates"]
                    )
                )

    def test_0226w_retires_outer_leads_before_clearing_deferred_inner_cars(self) -> None:
        stage2_dir = ROOT / "artifacts" / "four_stage_balanced_current" / "stage2"
        stage2_path = stage2_dir / "0226W_combined_response.json"
        if not stage2_path.exists():
            self.skipTest("local 0226W stage2 regression artifact is not available")
        request_path = next((ROOT / "data" / "truth2").glob("*0226W.json"))
        req = json.loads(request_path.read_text(encoding="utf-8-sig"))
        stage2 = json.loads(stage2_path.read_text(encoding="utf-8"))

        result = Stage3Solver("0226W", req, stage2, time_budget_seconds=5).solve()
        summary = result["summary"]
        trace = result["trace"]
        deferred_get_index = next(
            row["index"]
            for row in trace
            if row["action"] == "Get" and row["line"] == "修4库内"
        )
        completed_before_clear = {
            row["line"]
            for row in trace
            if row["index"] < deferred_get_index
            and row["action"] == "Put"
            and row["line"] in {"修1库内", "修3库内"}
        }

        self.assertEqual(summary["operations"], 10)
        self.assertEqual(summary["operation_lower_bound"], 10)
        self.assertEqual(summary["evaluated_strategy_portfolio_gap"], 0)
        self.assertEqual(completed_before_clear, {"修1库内", "修3库内"})
        self.assertEqual(rv.replay(result["stage3_request"], result["response"])[1], [])
        self.assertEqual(rv.replay(req, result["combined_response"])[1], [])


if __name__ == "__main__":
    unittest.main()
