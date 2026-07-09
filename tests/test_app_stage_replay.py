from __future__ import annotations

import sys
import types


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
