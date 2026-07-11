from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path


class _DummyStreamlit(types.ModuleType):
    def cache_data(self, *args, **kwargs):
        def decorator(fn):
            return fn

        return decorator

    def cache_resource(self, *args, **kwargs):
        def decorator(fn):
            return fn

        return decorator

    def __getattr__(self, _name):
        def noop(*args, **kwargs):
            return None

        return noop


sys.modules.setdefault("streamlit", _DummyStreamlit("streamlit"))

import app  # noqa: E402


def test_replay_put_frame_keeps_remaining_train_cars() -> None:
    payload = {
        "StartStatus": [
            {"No": "A", "Line": "存2线", "Position": 1, "Length": 12.1, "TargetLines": ["调梁棚"]},
            {"No": "B", "Line": "存2线", "Position": 2, "Length": 12.1, "TargetLines": ["存2线"]},
            {"No": "C", "Line": "存2线", "Position": 3, "Length": 12.1, "TargetLines": ["预修线"]},
        ],
        "locoNode": {"Line": "机走北", "End": "North"},
    }
    response = {
        "Data": {
            "Operations": [
                {
                    "Index": 1,
                    "Action": "Get",
                    "Line": "存2线",
                    "MoveCars": ["A", "B", "C"],
                    "TrainCars": ["A", "B", "C"],
                    "PassbyPath": ["机走北", "存2线"],
                },
                {
                    "Index": 2,
                    "Action": "Put",
                    "Line": "预修线",
                    "MoveCars": ["C"],
                    "TrainCars": ["A", "B"],
                    "PassbyPath": ["存2线", "预修线"],
                },
                {
                    "Index": 3,
                    "Action": "Put",
                    "Line": "存2线",
                    "MoveCars": ["B"],
                    "TrainCars": ["A"],
                    "PassbyPath": ["预修线", "存2线"],
                },
                {
                    "Index": 4,
                    "Action": "Put",
                    "Line": "调梁棚",
                    "MoveCars": ["A"],
                    "TrainCars": [],
                    "PassbyPath": ["存2线", "调梁棚"],
                },
            ]
        }
    }

    rows = app._stage1_response_operation_rows(response)
    frames = app._p10_build_replay_frames(payload, rows, response)

    assert frames[2]["action"] == "Put"
    assert frames[2]["move_cars"] == ["C"]
    assert frames[2]["train_cars"] == ["A", "B"]
    assert frames[3]["action"] == "Put"
    assert frames[3]["move_cars"] == ["B"]
    assert frames[3]["train_cars"] == ["A"]
    assert frames[4]["action"] == "Put"
    assert frames[4]["move_cars"] == ["A"]
    assert frames[4]["train_cars"] == []


def test_truth_payload_loader_supports_truth3_cases() -> None:
    payload = {"StartStatus": [{"No": "A", "Line": "存1线", "Position": 1}]}
    original_truth2 = app.TRUTH2_DIR
    original_truth3 = app.TRUTH3_DIR
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        truth2 = root / "truth2"
        truth3 = root / "truth3"
        truth2.mkdir()
        truth3.mkdir()
        (truth3 / "validation_取送车计划_20260401W.json").write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        try:
            app.TRUTH2_DIR = truth2
            app.TRUTH3_DIR = truth3
            loaded = app._stage1_load_truth_payload("0401W")
        finally:
            app.TRUTH2_DIR = original_truth2
            app.TRUTH3_DIR = original_truth3

    assert loaded == payload


def test_stage4_combined_response_builds_full_flow_frames() -> None:
    payload = {
        "StartStatus": [
            {"No": "A", "Line": "存1线", "Position": 1, "Length": 12.1, "TargetLines": ["预修线"]}
        ],
        "locoNode": {"Line": "机走北", "End": "North"},
    }
    combined_response = {
        "Data": {
            "Operations": [
                {
                    "Index": 1,
                    "Action": "Get",
                    "Line": "存1线",
                    "MoveCars": ["A"],
                    "TrainCars": ["A"],
                    "PassbyPath": ["机走北", "存1线"],
                },
                {
                    "Index": 2,
                    "Action": "Put",
                    "Line": "预修线",
                    "MoveCars": ["A"],
                    "TrainCars": [],
                    "PassbyPath": ["存1线", "预修线"],
                },
            ],
            "GeneratedEndStatus": [{"No": "A", "Line": "预修线", "Position": 1}],
        }
    }
    operation_rows = app._stage1_response_operation_rows(combined_response)
    frames = app._p10_build_replay_frames(payload, operation_rows, combined_response)

    assert len(frames) == len(operation_rows) + 2
    assert frames[0]["title"] == "初始状态"
    assert frames[-1]["action"] == "Final"


def test_fullflow_stage_boundaries_cover_every_operation() -> None:
    def response(count: int) -> dict:
        return {
            "Data": {
                "Operations": [
                    {"Index": index, "Action": "Get", "Line": "存1线"}
                    for index in range(1, count + 1)
                ]
            }
        }

    boundaries, stage_sequence = app._fullflow_stage_boundaries(
        {
            "stage1": response(2),
            "stage2": response(1),
            "stage3": response(0),
            "stage4": response(3),
        }
    )

    assert [row["operationRange"] for row in boundaries] == ["1-2", "3-3", "无", "4-6"]
    assert stage_sequence == ["第一阶段", "第一阶段", "第二阶段", "第四阶段", "第四阶段", "第四阶段"]
