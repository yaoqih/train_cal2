from scripts.analyze_stage4_coherence import (
    analyze_case,
    manual_goal_compatibility,
    sequence_metrics,
)


def operation(index, action, line, move, train):
    return {
        "Index": index,
        "Action": action,
        "Line": line,
        "MoveCars": move,
        "TrainCars": train,
        "PassbyPath": [line],
    }


def test_sequence_metrics_distinguish_multi_get_from_productive_partial_puts():
    operations = [
        operation(1, "Get", "A", ["a", "b"], ["a", "b"]),
        operation(2, "Get", "B", ["c"], ["a", "b", "c"]),
        operation(3, "Put", "X", ["c"], ["a", "b"]),
        operation(4, "Put", "Y", ["a", "b"], []),
        operation(5, "Get", "X", ["c"], ["c"]),
        operation(6, "Put", "Z", ["c"], []),
    ]

    metrics = sequence_metrics(operations)

    assert metrics.multi_get_put_hooks == 1
    assert metrics.multi_source_get_runs == 1
    assert metrics.partial_put_hooks == 1
    assert metrics.multi_put_runs == 1
    assert metrics.max_get_run == 2
    assert metrics.max_put_run == 2
    assert metrics.repeated_get_car_moves == 1
    assert metrics.singleton_put_hooks == 2


def test_case_metrics_replay_temporary_cycle_and_zero_net_relocation():
    operations = [
        operation(1, "Get", "S", ["a", "b"], ["a", "b"]),
        operation(2, "Put", "S", ["b"], ["a"]),
        operation(3, "Get", "S", ["b"], ["a", "b"]),
        operation(4, "Put", "Y", ["b"], ["a"]),
        operation(5, "Put", "X", ["a"], []),
        operation(6, "Get", "X", ["a"], ["a"]),
        operation(7, "Put", "X", ["a"], []),
    ]
    response = {
        "Data": {
            "Operations": operations,
            "GeneratedEndStatus": [
                {"No": "a", "Line": "X"},
                {"No": "b", "Line": "Y"},
            ],
        }
    }
    summary = {
        "active_nos": ["a", "b"],
        "business_hooks": 7,
        "hook_lower_bound": 2,
        "hook_optimality_gap": 5,
        "objective": [7, 2, 1, 0],
        "search_stop_reason": "label_frontier_exhausted",
        "evaluated_labels": 1,
    }
    trace = [
        {"event": "operation", "action": item["Action"], "hooks": item["Index"]}
        for item in operations
    ]

    metrics = analyze_case("synthetic", summary, response, trace)

    assert metrics.repeated_get_car_moves == 2
    assert metrics.temporary_put_car_moves == 1
    assert metrics.temporary_put_hooks == 1
    assert metrics.temporary_recovery_get_hooks == 1
    assert metrics.pure_temporary_cycle_hooks == 2
    assert metrics.zero_net_relocation_car_moves == 1
    assert metrics.active_zero_net_relocation_car_moves == 1
    assert metrics.target_reopens == 1
    assert metrics.reopened_targets == {"X": 1}


def test_manual_goal_compatibility_distinguishes_out_of_domain_targets(tmp_path):
    stage4 = tmp_path / "stage4" / "truth2"
    manual = tmp_path / "manual"
    case_id = "0001W"
    stage4.mkdir(parents=True)
    manual.mkdir()
    (stage4 / f"{case_id}_summary.json").write_text(
        '{"active_nos":["A"],"business_hooks":3,"hook_optimality_gap":1}',
        encoding="utf-8",
    )
    (stage4 / f"{case_id}_response.json").write_text(
        '{"Data":{"GeneratedEndStatus":[{"No":"A","Line":"算法线"}]}}',
        encoding="utf-8",
    )
    (stage4 / f"{case_id}_stage4_request.json").write_text(
        '{"StartStatus":[{"No":"A","TargetLines":["算法线"]}]}',
        encoding="utf-8",
    )
    (manual / f"{case_id}_bundle.json").write_text(
        '{"Response":{"Data":{"GeneratedEndStatus":'
        '[{"No":"A","Line":"人工线"}]}}}',
        encoding="utf-8",
    )

    result = manual_goal_compatibility(tmp_path / "stage4", manual)

    assert result["different_within_stage4_domain"] == 0
    assert result["manual_outside_stage4_domain"] == 1
    assert result["stage4_target_domain_size_counts"] == {"1": 1}
