from __future__ import annotations

from pathlib import Path

from scripts.convert_april_plans_to_truth import (
    SourceCar,
    split_legacy_paired_targets,
    validate_line_slot_capacities,
)


def source_car(
    row: int,
    line: str,
    target: str,
    number: str,
    *,
    spotting: str = "",
) -> SourceCar:
    return SourceCar(
        source_file=Path("legacy.xlsx"),
        row=row,
        block="A:J",
        line=line,
        position=row,
        car_type="X70",
        number=number,
        repair_process="段",
        remark="",
        target_line=target,
        spotting=spotting,
        attribute="",
    )


def converted_car(no: str, line: str, target: str, *, forced: bool = False) -> dict:
    car = {
        "No": no,
        "Line": line,
        "Position": 1,
        "TargetLines": [target],
    }
    if forced:
        car["ForceTargetPosition"] = [1, 2, 3]
    return car


def test_legacy_wash_south_overflow_preserves_contiguous_source_groups() -> None:
    sources: list[SourceCar] = []
    cars: list[dict] = []

    for index in range(4):
        no = f"S{index}"
        sources.append(source_car(10 + index, "洗南", "", no))
        cars.append(converted_car(no, "洗罐站", "洗罐站"))
    for index in range(2):
        no = f"O{index}"
        sources.append(source_car(20 + index, "油", "洗南", no))
        cars.append(converted_car(no, "油漆线", "洗罐站"))
    for index in range(6):
        no = f"C{index}"
        sources.append(source_car(30 + index, "存3", "洗南", no))
        cars.append(converted_car(no, "存3线", "洗罐站"))

    corrections: list[str] = []
    split_legacy_paired_targets(sources, cars, corrections)

    assert [car["TargetLines"][0] for car in cars[:6]] == ["洗罐站"] * 6
    assert [car["TargetLines"][0] for car in cars[6:]] == ["洗罐线北"] * 6
    assert len(corrections) == 6


def test_line_slot_validation_rejects_unsplit_directional_target() -> None:
    cars = [
        converted_car(str(index), "存3线", "洗罐站")
        for index in range(8)
    ]

    try:
        validate_line_slot_capacities(cars, Path("overflow.json"))
    except ValueError as exc:
        assert "target line 洗罐站 has 8 cars but only 7 workbook slots" in str(exc)
    else:
        raise AssertionError("expected slot-capacity validation to reject wash overflow")
