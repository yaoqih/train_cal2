from __future__ import annotations

from pathlib import Path
import sys

import replay_validator as rv


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from solver_vnext import physical


def _car(no: str, line: str, position: int, target: str = "调梁棚") -> dict:
    return physical.normalized_car(
        {
            "No": no,
            "Line": line,
            "Position": position,
            "Length": 14.3,
            "TargetLines": [target],
            "ForceTargetPosition": [6, 7, 8, 9] if target == "调梁棚" else [],
        }
    )


def test_physical_rejects_explicit_position_put_behind_existing_north_car() -> None:
    cars = [
        _car("OLD1", "调梁棚", 1),
        _car("OLD2", "调梁棚", 2),
        _car("NEW1", "", 0),
        _car("NEW2", "", 0),
    ]
    planned_positions = {"NEW1": 6, "NEW2": 7}
    candidate = physical.HookCandidate(
        case_id="TEST",
        hook_index=1,
        candidate_id="TEST:insert",
        source_line="存2线",
        target_line="调梁棚",
        move_car_nos=("NEW1", "NEW2"),
        action_family="target_move",
        train_length_m=28.6,
        pull_equivalent_count=2,
        has_weigh=False,
        planned_positions=planned_positions,
        generation_reason="test",
    )
    projected = physical.projected_after_physical_put(
        cars,
        "调梁棚",
        ["NEW1", "NEW2"],
        planned_positions,
    )

    reasons = physical.validate_target_positions(
        candidate,
        projected,
        [cars[2], cars[3]],
        physical.DepotAssignment(slots={}, failures={}),
    )

    assert any(reason.startswith("business_position_put_blocked_by_access_end:") for reason in reasons)


def test_replay_rejects_explicit_position_put_behind_existing_north_car() -> None:
    request = {
        "StartStatus": [
            {"No": "OLD1", "Line": "调梁棚", "Position": 1, "Length": 14.3, "TargetLines": ["调梁棚"]},
            {"No": "NEW1", "Line": "存2线", "Position": 1, "Length": 14.3, "TargetLines": ["调梁棚"]},
            {"No": "NEW2", "Line": "存2线", "Position": 2, "Length": 14.3, "TargetLines": ["调梁棚"]},
        ],
        "TerminalLines": [],
        "locoNode": {"Line": "存2线", "End": "North"},
    }
    response = {
        "Data": {
            "Operations": [
                {
                    "Index": 1,
                    "Action": "Get",
                    "Line": "存2线",
                    "MoveCars": ["NEW1", "NEW2"],
                    "TrainCars": ["NEW1", "NEW2"],
                    "PassbyPath": ["存2线"],
                },
                {
                    "Index": 2,
                    "Action": "Put",
                    "Line": "调梁棚",
                    "MoveCars": ["NEW1", "NEW2"],
                    "TrainCars": [],
                    "PassbyPath": ["存2线", "渡3", "渡2", "机北1", "机北2", "渡4", "调梁线北", "调梁棚"],
                    "Positions": {"NEW1": 6, "NEW2": 7},
                },
            ]
        }
    }

    _cars, violations = rv.replay(request, response)

    assert any(v.code == "put_position_blocked_by_access_end" for v in violations)
