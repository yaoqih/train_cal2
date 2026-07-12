from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import importlib
import json
from pathlib import Path
from html import escape
import re
import sys

import streamlit as st

ROOT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
TRUTH2_DIR = ROOT_DIR / "data" / "truth2"
TRUTH3_DIR = ROOT_DIR / "data" / "truth3"
SCHEMATIC_LAYOUT_PATH = ROOT_DIR / "data" / "map" / "schematic_layout.json"
MANUAL_RESTORE_DIR = ROOT_DIR / "artifacts" / "manual_restored_interface"
DEFAULT_FULLFLOW_ARTIFACT_DIR = ROOT_DIR / "artifacts" / "fullflow_current"
DEFAULT_STAGE1_SIMPLE_DIR = DEFAULT_FULLFLOW_ARTIFACT_DIR / "truth2" / "stage1"
DEFAULT_STAGE2_SIMPLE_DIR = DEFAULT_FULLFLOW_ARTIFACT_DIR / "truth2" / "stage2"
DEFAULT_STAGE3_SIMPLE_DIR = DEFAULT_FULLFLOW_ARTIFACT_DIR / "truth2" / "stage3"
DEFAULT_STAGE4_SIMPLE_DIR = DEFAULT_FULLFLOW_ARTIFACT_DIR / "truth2" / "stage4"
BUSINESS_HOOK_ACTIONS = {"Get", "Put"}
STAGE_SIMPLE_OUTPUT_DIRS = {
    1: DEFAULT_STAGE1_SIMPLE_DIR,
    2: DEFAULT_STAGE2_SIMPLE_DIR,
    3: DEFAULT_STAGE3_SIMPLE_DIR,
    4: DEFAULT_STAGE4_SIMPLE_DIR,
}


@dataclass(frozen=True)
class ReplayOperationRow:
    hook_index: int
    operation_index: int
    action: str
    line: str
    move_cars: str
    train_cars: str
    passby_path: str


def _vnext_physical_module():
    scripts_path = str(SCRIPTS_DIR)
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)
    return importlib.import_module("solver_vnext.physical")


def main():
    st.set_page_config(page_title="福州东四阶段调车", layout="wide")
    st.title("福州东四阶段调车")

    fullflow_tab, manual_tab, stage1_tab, stage2_tab, stage3_tab, stage4_tab = st.tabs(
        [
            "全流程回放",
            "人工计划回放",
            "第一阶段可视化",
            "第二阶段可视化",
            "第三阶段可视化",
            "第四阶段可视化",
        ]
    )
    with fullflow_tab:
        _render_fullflow_replay_dashboard()
    with manual_tab:
        _render_manual_restored_dashboard()
    with stage1_tab:
        _render_stage1_simple_dashboard()
    with stage2_tab:
        _render_stage2_simple_dashboard()
    with stage3_tab:
        _render_stage3_simple_dashboard()
    with stage4_tab:
        _render_stage4_simple_dashboard()


def _replay_try_case_id_from_text(text: str) -> str | None:
    match = re.search(r"(\d{4}[ZWzw])", text or "")
    return match.group(1).upper() if match else None


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _read_json_object(path: Path) -> dict:
    value = _read_json(path)
    if not isinstance(value, dict):
        raise ValueError("JSON 根节点必须是对象")
    return value



@st.cache_data(show_spinner=False)
def _manual_restored_bundle_options() -> list[str]:
    bundle_dir = MANUAL_RESTORE_DIR / "bundles"
    if not bundle_dir.exists():
        return []
    paths = sorted(bundle_dir.glob("*.json"), key=lambda path: (_replay_try_case_id_from_text(path.name) or path.name, path.name))
    return [str(path) for path in paths]


def _render_manual_restored_dashboard() -> None:
    st.subheader("人工计划回放")
    st.caption("读取人工调车 Excel 还原出的接口响应 bundle，按 Operations 回放人工计划。")
    options = _manual_restored_bundle_options()
    if not options:
        st.warning(
            "还没有人工计划还原结果。先运行："
            ".venv/bin/python scripts/restore_manual_interface_responses.py "
            "--root . --output-dir artifacts/manual_restored_interface"
        )
        return

    selected_path_text = st.selectbox(
        "人工计划 bundle",
        options=options,
        format_func=_manual_restored_bundle_label,
        key="manual-restored-bundle",
    )
    bundle_path = Path(selected_path_text)
    try:
        bundle = _read_json_object(bundle_path)
    except Exception as exc:  # noqa: BLE001
        st.error(f"人工计划 bundle 读取失败：{exc}")
        return

    request_payload = bundle.get("Request") or {"StartStatus": [], "locoNode": {}}
    response = bundle.get("Response") or {}
    summary = bundle.get("Summary") or {}
    trace_rows = bundle.get("Trace") or []
    vehicle_display_labels = _replay_vehicle_display_labels(request_payload)
    operation_rows = _manual_response_operation_rows(response)
    success = bool(response.get("Success"))
    if success:
        st.success("人工计划响应已按人工同线口径完整还原。")
    else:
        st.warning(f"人工计划响应为部分还原：{response.get('Message') or summary.get('blocked_reason') or '未知原因'}")
    st.caption("说明：该视图用于观察人工计划；人工同线组会跨越物理模型中的多段股道，因此不等同于 vNext 严格物理可执行结果。")

    cols = st.columns(6)
    cols[0].metric("案例", summary.get("case_id", _replay_try_case_id_from_text(bundle_path.name) or ""))
    cols[1].metric("人工勾数", summary.get("manual_hook_count", 0))
    cols[2].metric("接口操作", summary.get("operation_count", len(operation_rows)))
    cols[3].metric("已还原", summary.get("restored_hook_count", 0))
    cols[4].metric("无动车/跳过", summary.get("noop_hook_count", 0))
    cols[5].metric("阻塞", summary.get("blocked_hook_count", 0))
    st.caption(f"bundle：{bundle_path}")

    view = st.radio(
        "查看内容",
        options=["可视化回放", "人工计划", "终态", "还原诊断", "原始 JSON"],
        horizontal=True,
        key="manual-restored-view",
    )
    if view == "可视化回放":
        _render_replay_replay(request_payload, operation_rows, response, vehicle_display_labels, key_prefix="manual")
    elif view == "人工计划":
        st.markdown("**接口操作序列（ManualHook 为原人工勾号）**")
        st.dataframe(_replay_operation_table_rows(operation_rows, vehicle_display_labels), width="stretch", hide_index=True)
        if trace_rows:
            st.markdown("**人工还原 Trace**")
            st.dataframe(_manual_trace_table_rows(trace_rows, vehicle_display_labels), width="stretch", hide_index=True)
    elif view == "终态":
        _render_replay_end_status(response, vehicle_display_labels)
    elif view == "还原诊断":
        st.markdown("**summary**")
        st.json(summary)
        status_counts = Counter(str(row.get("status") or "") for row in trace_rows)
        if status_counts:
            st.markdown("**状态分布**")
            st.dataframe(
                [{"status": status, "count": count} for status, count in sorted(status_counts.items())],
                width="stretch",
                hide_index=True,
            )
        if trace_rows:
            st.markdown("**Trace 明细**")
            st.dataframe(_manual_trace_table_rows(trace_rows, vehicle_display_labels), width="stretch", hide_index=True)
    else:
        left, right = st.columns(2)
        with left:
            st.markdown("**Request**")
            st.json(_replay_response_for_display(request_payload, vehicle_display_labels))
            st.markdown("**Summary**")
            st.json(summary)
        with right:
            st.markdown("**Response**")
            st.json(_replay_response_for_display(response, vehicle_display_labels))


def _manual_restored_bundle_label(path_text: str) -> str:
    path = Path(path_text)
    case_id = _replay_try_case_id_from_text(path.name) or "未知案例"
    return f"{case_id} | {path.name}"


def _manual_trace_table_rows(trace_rows: list[dict], vehicle_display_labels: dict[str, str]) -> list[dict[str, object]]:
    rows = []
    for row in trace_rows:
        rows.append(
            {
                "manualHook": row.get("manual_hook", ""),
                "operationIndex": row.get("operation_index", ""),
                "status": row.get("status", ""),
                "raw": f"{row.get('line_raw', '')}{row.get('method', '')}{row.get('effective_count') or row.get('count') or ''}",
                "note": row.get("note", ""),
                "operation": row.get("operation_action", ""),
                "line": row.get("operation_line", ""),
                "moveCars": _replay_format_vehicle_pipe(row.get("move_cars", ""), vehicle_display_labels),
                "trainCars": _replay_format_vehicle_pipe(row.get("train_cars", ""), vehicle_display_labels),
                "candidate": row.get("candidate_validation", ""),
                "detail": _replay_annotate_known_vehicle_text(row.get("detail", ""), vehicle_display_labels),
            }
        )
    return rows


def _replay_vehicle_display_labels(payload: dict) -> dict[str, str]:
    labels: dict[str, str] = {}
    for car in payload.get("StartStatus") or []:
        no = str(car.get("No") or "").strip()
        if not no:
            continue
        attributes = _replay_vehicle_display_attributes(car)
        labels[no] = f"{no}({';'.join(attributes)})"
    return labels


def _replay_vehicle_display_attributes(car: dict) -> list[str]:
    attributes: list[str] = []
    target_lines = [
        str(line).strip()
        for line in car.get("TargetLines") or []
        if str(line).strip()
    ]
    display_targets = _replay_display_target_lines(target_lines)
    attributes.append("/".join(display_targets) if display_targets else "无目标")

    repair_process = str(car.get("RepairProcess") or "").strip()
    if repair_process:
        attributes.append(repair_process)

    length_text = _replay_format_number(car.get("Length"))
    if length_text:
        attributes.append(f"{length_text}m")

    if _replay_as_bool(car.get("IsHeavy")):
        attributes.append("重")
    if _replay_as_bool(car.get("IsWeigh")):
        attributes.append("称重")
    if _replay_as_bool(car.get("IsClosedDoor")):
        attributes.append("关门")

    force_positions = [
        str(position).strip()
        for position in car.get("ForceTargetPosition") or []
        if str(position).strip()
    ]
    if force_positions:
        attributes.append(f"位{'/'.join(force_positions)}")
    return attributes


def _replay_display_target_lines(target_lines: list[str]) -> list[str]:
    display_targets: list[str] = []
    for line in target_lines:
        display_line = "大库内" if line in {"修1库内", "修2库内", "修3库内", "修4库内"} else line
        if display_line not in display_targets:
            display_targets.append(display_line)
    return display_targets


def _replay_format_number(value) -> str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def _replay_as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "是"}
    return bool(value)


def _replay_vehicle_label(car_no, vehicle_display_labels: dict[str, str]) -> str:
    no = str(car_no or "").strip()
    if not no:
        return ""
    return vehicle_display_labels.get(no, f"{no}(目标:未知)")


def _replay_compact_vehicle_label(car_no, vehicle_display_labels: dict[str, str]) -> str:
    no = str(car_no or "").strip()
    if not no:
        return ""
    full_label = _replay_vehicle_label(no, vehicle_display_labels)
    target_match = re.search(r"\(([^;)]+)", full_label)
    target_text = target_match.group(1) if target_match else "?"
    short_target = target_text.split("/")[0]
    return f"{no}({short_target})"


def _replay_format_vehicle_list(car_nos: list[str], vehicle_display_labels: dict[str, str]) -> str:
    return " ".join(
        label
        for label in (_replay_vehicle_label(car_no, vehicle_display_labels) for car_no in car_nos)
        if label
    )


def _replay_format_vehicle_pipe(text: str, vehicle_display_labels: dict[str, str]) -> str:
    return _replay_format_vehicle_list(_replay_split_pipe(text), vehicle_display_labels)


def _replay_annotate_known_vehicle_text(text, vehicle_display_labels: dict[str, str]) -> str:
    result = str(text or "")
    for car_no, label in sorted(vehicle_display_labels.items(), key=lambda item: len(item[0]), reverse=True):
        result = re.sub(
            rf"(?<![0-9A-Za-z]){re.escape(car_no)}(?![0-9A-Za-z])",
            label,
            result,
        )
    return result


def _replay_response_for_display(value, vehicle_display_labels: dict[str, str]):
    if isinstance(value, list):
        return [_replay_response_for_display(item, vehicle_display_labels) for item in value]
    if not isinstance(value, dict):
        return value

    result = {}
    for key, item in value.items():
        if key in {"MoveCars", "TrainCars"} and isinstance(item, list):
            result[key] = [
                _replay_vehicle_label(car_no, vehicle_display_labels)
                for car_no in item
            ]
        elif key == "No":
            result[key] = _replay_vehicle_label(item, vehicle_display_labels)
        else:
            result[key] = _replay_response_for_display(item, vehicle_display_labels)
    return result


def _replay_hook_summary_rows(operation_rows, vehicle_display_labels: dict[str, str]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for hook_index, group in _replay_group_operations_by_hook(operation_rows).items():
        get_op = next((row for row in group if row.action == "Get"), group[0])
        put_op = next((row for row in reversed(group) if row.action == "Put"), group[-1])
        move_cars = _replay_split_pipe(get_op.move_cars)
        route: list[str] = []
        for op in group:
            route = _replay_extend_route(route, _replay_split_pipe(op.passby_path))
        rows.append(
            {
                "moveBatch": hook_index,
                "source": get_op.line,
                "target": put_op.line,
                "carCount": len(move_cars),
                "moveCars": _replay_format_vehicle_list(move_cars, vehicle_display_labels),
                "hasWeigh": any(row.action == "Weigh" for row in group),
                "businessHookCount": sum(row.action in BUSINESS_HOOK_ACTIONS for row in group),
                "operationCount": len(group),
                "route": " -> ".join(route),
            }
        )
    return rows


def _replay_operation_table_rows(operation_rows, vehicle_display_labels: dict[str, str]) -> list[dict[str, object]]:
    rows = []
    business_hook_no = 0
    for row in sorted(operation_rows, key=lambda item: (item.hook_index, item.operation_index)):
        if row.action in BUSINESS_HOOK_ACTIONS:
            business_hook_no += 1
            display_business_hook_no = str(business_hook_no)
        else:
            display_business_hook_no = ""
        rows.append(
            {
                "businessHookNo": display_business_hook_no,
                "moveBatch": row.hook_index,
                "operationIndex": row.operation_index,
                "action": row.action,
                "line": row.line,
                "moveCars": _replay_format_vehicle_pipe(row.move_cars, vehicle_display_labels),
                "trainCars": _replay_format_vehicle_pipe(row.train_cars, vehicle_display_labels),
                "passbyPath": " -> ".join(_replay_split_pipe(row.passby_path)),
            }
        )
    return rows


def _replay_group_operations_by_hook(operation_rows) -> dict[int, list]:
    grouped: dict[int, list] = defaultdict(list)
    for row in operation_rows:
        grouped[int(row.hook_index)].append(row)
    return {
        hook_index: sorted(rows, key=lambda row: row.operation_index)
        for hook_index, rows in sorted(grouped.items())
    }


def _render_replay_replay(
    payload: dict,
    operation_rows,
    response: dict,
    vehicle_display_labels: dict[str, str],
    key_prefix: str = "p10",
    operation_stage_labels: dict[int, str] | None = None,
) -> None:
    frames = _replay_build_replay_frames(payload, operation_rows, response)
    if not frames:
        st.info("当前没有可回放状态。")
        return
    if operation_stage_labels:
        for frame in frames:
            operation_index = frame.get("operation")
            if operation_index:
                stage_label = operation_stage_labels.get(int(operation_index), "")
            elif frame.get("action") == "Final":
                stage_label = "全流程终态"
            else:
                stage_label = "原始起点"
            frame["stage"] = stage_label
            if stage_label and operation_index:
                frame["title"] = f"{stage_label} | {frame['title']}"
    vehicle_target_tracks = _replay_vehicle_target_tracks(payload)
    max_frame_index = len(frames) - 1
    frame_key = f"{key_prefix}_replay_frame_index"
    if frame_key not in st.session_state:
        st.session_state[frame_key] = 0
    if int(st.session_state[frame_key]) > max_frame_index:
        st.session_state[frame_key] = max_frame_index
    st.markdown('<div id="replay-replay-anchor"></div>', unsafe_allow_html=True)
    frame_index = st.slider(
        "回放步骤（按接口操作推进）",
        min_value=0,
        max_value=max_frame_index,
        key=frame_key,
    )
    st.caption(f"回放位置：{frame_index + 1}/{len(frames)}")
    frame = frames[frame_index]
    st.markdown(f"**{frame['title']}**")
    st.caption(frame["detail"] or "初始股道状态")

    canvas_col, side_col = st.columns([7, 3])
    with canvas_col:
        view_mode = st.radio(
            "可视化模式",
            options=["线路拓扑", "股道占用"],
            horizontal=True,
            key=f"{key_prefix}-replay-view-mode",
        )
        if view_mode == "线路拓扑":
            st.markdown(
                _replay_topology_svg(
                    state_by_line=frame["state"],
                    active_path=frame["path"],
                    source_line=frame["source_line"],
                    target_line=frame["target_line"],
                    active_line=frame["active_line"],
                    move_cars=frame["move_cars"],
                    train_cars=frame["train_cars"],
                    vehicle_target_tracks=vehicle_target_tracks,
                ),
                unsafe_allow_html=True,
            )
            st.caption("蓝色实线=本步骤经过线路/道岔分支；橙色=取车线；绿色=放车线；圆点绿色/橙色=已到位/未到位车辆数。")
        else:
            st.markdown(
                _replay_yard_svg(
                    state_by_line=frame["state"],
                    active_path=set(frame["path"]),
                    source_line=frame["source_line"],
                    target_line=frame["target_line"],
                    move_cars=frame["move_cars"],
                    train_cars=frame["train_cars"],
                    vehicle_display_labels=vehicle_display_labels,
                ),
                unsafe_allow_html=True,
            )
        st.markdown(
            _replay_route_chips_html(frame["path"]),
            unsafe_allow_html=True,
        )
    with side_col:
        st.markdown(
            _replay_replay_detail_html(
                frame,
                frame_index=frame_index,
                frame_count=len(frames),
                vehicle_display_labels=vehicle_display_labels,
            ),
            unsafe_allow_html=True,
        )

    state_rows = _replay_state_table_rows(
        frame["state"],
        highlighted_lines=set(frame["path"]) | {frame["source_line"], frame["target_line"]},
        vehicle_display_labels=vehicle_display_labels,
    )
    if state_rows:
        st.markdown(_replay_cars_table_html(state_rows), unsafe_allow_html=True)


def _replay_build_replay_frames(payload: dict, operation_rows, response: dict) -> list[dict]:
    physical = _vnext_physical_module()
    cars = [physical.normalized_car(car) for car in payload.get("StartStatus") or []]
    state = _replay_state_from_physical_cars(cars, physical)
    loco = payload.get("locoNode") or {}
    frames: list[dict] = [
        {
            "title": "初始状态",
            "detail": f"机车位置：{loco.get('Line') or '未知'} / {loco.get('End') or '未知'}",
            "state": _replay_copy_state(state),
            "hook": "",
            "operation": "",
            "action": "",
            "active_line": str(loco.get("Line") or ""),
            "source_line": "",
            "target_line": "",
            "path": [],
            "move_cars": [],
            "train_cars": [],
            "business_hook": "",
        }
    ]

    train_cars: list[str] = []
    carried_order: list[str] = []
    business_hook_no = 0
    for row in sorted(operation_rows, key=lambda item: (item.hook_index, item.operation_index)):
        action = row.action
        line = physical.normalize_line(row.line)
        move_cars = _replay_split_pipe(row.move_cars)
        path = _replay_split_pipe(row.passby_path)
        source_line = ""
        target_line = ""
        display_business_hook_no: int | str = ""

        if action == "Get":
            business_hook_no += 1
            display_business_hook_no = business_hook_no
            move_set = set(move_cars)
            carried_set = set(carried_order)
            for car_no in physical.carried_order_after_get(
                cars=cars,
                line=line,
                move_nos=move_set,
                carried_nos=carried_set,
            ):
                if car_no not in carried_order:
                    carried_order.append(car_no)
            physical.apply_physical_get_order(cars, line, move_cars)
            train_cars = _replay_split_pipe(row.train_cars) or list(carried_order)
            source_line = line
        elif action == "Weigh":
            train_cars = _replay_split_pipe(row.train_cars) or train_cars or move_cars
            move_set = set(move_cars)
            for car in cars:
                if physical.car_no(car) in move_set:
                    car["_Weighed"] = True
        elif action == "Put":
            business_hook_no += 1
            display_business_hook_no = business_hook_no
            if move_cars and len(carried_order) >= len(move_cars):
                put_order = carried_order[-len(move_cars):]
            else:
                put_order = list(move_cars)
            if set(put_order) != set(move_cars):
                put_order = list(move_cars)
            physical.apply_physical_put_order(cars, line, put_order)
            move_set = set(move_cars)
            carried_order = [car_no for car_no in carried_order if car_no not in move_set]
            train_cars = _replay_split_pipe(row.train_cars) or list(carried_order)
            target_line = line
        state = _replay_state_from_physical_cars(cars, physical)

        frames.append(
            {
                "title": _replay_replay_frame_title(row, display_business_hook_no),
                "detail": f"{line} | 路径：{' -> '.join(path) if path else '无'}",
                "state": _replay_copy_state(state),
                "hook": row.hook_index,
                "operation": row.operation_index,
                "action": action,
                "active_line": line,
                "source_line": source_line,
                "target_line": target_line,
                "path": path,
                "move_cars": move_cars,
                "train_cars": list(train_cars),
                "business_hook": display_business_hook_no,
            }
        )

    generated = ((response or {}).get("Data") or {}).get("GeneratedEndStatus") or []
    if generated:
        frames.append(
            {
                "title": "接口 GeneratedEndStatus 终态",
                "detail": "最终车辆位置以接口响应为准。",
                "state": _replay_state_from_status(generated),
                "hook": "",
                "operation": "",
                "action": "Final",
                "active_line": "",
                "source_line": "",
                "target_line": "",
                "path": [],
                "move_cars": [],
                "train_cars": [],
                "business_hook": "",
            }
        )
    return frames


def _replay_replay_frame_title(row, business_hook_no: int | str) -> str:
    if business_hook_no:
        return (
            f"业务第 {business_hook_no} 勾 / 内部移动批次 {row.hook_index} / "
            f"接口操作 {row.operation_index}: {row.action}"
        )
    return f"内部移动批次 {row.hook_index} / 接口操作 {row.operation_index}: {row.action}"


def _replay_replay_detail_html(
    frame: dict,
    *,
    frame_index: int,
    frame_count: int,
    vehicle_display_labels: dict[str, str],
) -> str:
    detail_rows = [
        ("步骤", f"{frame_index + 1}/{frame_count}", False),
        ("业务勾号", frame["business_hook"] or "无", False),
        ("内部移动批次", frame["hook"] or "无", False),
        ("接口操作序号", frame["operation"] or "无", False),
        ("动作", frame["action"] or "初始", False),
        ("股道", frame["active_line"] or "无", False),
        ("移动车辆", frame["move_cars"], True),
        ("调车机后挂", frame["train_cars"], True),
    ]
    if frame.get("stage"):
        detail_rows.insert(1, ("阶段", frame["stage"], False))
    rows_html = []
    for label, value, multiline in detail_rows:
        value_html = (
            _replay_vehicle_lines_html(value, vehicle_display_labels)
            if multiline
            else escape(str(value))
        )
        value_class = "replay-detail-value replay-detail-value-multiline" if multiline else "replay-detail-value"
        rows_html.append(
            "<div class='replay-detail-row'>"
            f"<div class='replay-detail-label'>{escape(label)}</div>"
            f"<div class='{value_class}'>{value_html}</div>"
            "</div>"
        )
    return f"""
    <style>
    .replay-detail-panel {{
      border: 1px solid #d9e2ec;
      border-radius: 8px;
      overflow: hidden;
      background: #ffffff;
    }}
    .replay-detail-row {{
      display: grid;
      grid-template-columns: minmax(86px, 34%) minmax(0, 1fr);
      border-bottom: 1px solid #e5edf5;
      min-height: 34px;
    }}
    .replay-detail-row:last-child {{
      border-bottom: 0;
    }}
    .replay-detail-label {{
      padding: 8px 10px;
      background: #f8fafc;
      color: #475569;
      font-size: 13px;
      font-weight: 700;
      line-height: 1.35;
    }}
    .replay-detail-value {{
      padding: 8px 10px;
      color: #0f172a;
      font-size: 13px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }}
    .replay-detail-value-multiline {{
      display: flex;
      flex-direction: column;
      gap: 6px;
      max-height: 360px;
      overflow: auto;
    }}
    .replay-detail-car {{
      display: block;
      padding: 3px 0;
      border-bottom: 1px dashed #e2e8f0;
      overflow-wrap: anywhere;
    }}
    .replay-detail-car:last-child {{
      border-bottom: 0;
    }}
    </style>
    <div class="replay-detail-panel">{''.join(rows_html)}</div>
    """


def _replay_vehicle_lines_html(car_nos: list[str], vehicle_display_labels: dict[str, str]) -> str:
    if not car_nos:
        return "无"
    return "".join(
        f"<span class='replay-detail-car'>{escape(_replay_vehicle_label(car_no, vehicle_display_labels))}</span>"
        for car_no in car_nos
    )


def _replay_state_from_physical_cars(cars: list[dict], physical) -> dict[str, list[str]]:
    state: dict[str, list[str]] = {}
    lines = sorted({car.get("Line") for car in cars if car.get("Line")})
    by_no = {physical.car_no(car): car for car in cars}
    for line in lines:
        ordered = [
            car_no
            for car_no in physical.line_access_order(cars, line)
            if car_no in by_no
        ]
        if ordered:
            state[line] = ordered
    return state


def _replay_state_from_status(status_rows: list[dict]) -> dict[str, list[str]]:
    rows = []
    for item in status_rows:
        no = str(item.get("No") or "").strip()
        line = str(item.get("Line") or "").strip()
        if no and line:
            rows.append((line, _replay_int_or_zero(item.get("Position")), no))
    state: dict[str, list[str]] = defaultdict(list)
    for line, _, no in sorted(rows):
        state[line].append(no)
    return dict(state)


def _replay_copy_state(state: dict[str, list[str]]) -> dict[str, list[str]]:
    return {line: list(cars) for line, cars in state.items()}


def _replay_vehicle_target_tracks(payload: dict) -> dict[str, set[str]]:
    targets: dict[str, set[str]] = {}
    for car in payload.get("StartStatus") or []:
        no = str(car.get("No") or "").strip()
        if not no:
            continue
        target_lines = {
            str(line).strip()
            for line in car.get("TargetLines") or []
            if str(line).strip()
        }
        targets[no] = target_lines
    return targets


def _replay_line_target_counts(
    line: str,
    cars: list[str],
    vehicle_target_tracks: dict[str, set[str]],
) -> tuple[int, int]:
    ok_count = 0
    pending_count = 0
    for car_no in cars:
        targets = vehicle_target_tracks.get(car_no) or set()
        if targets and line in targets:
            ok_count += 1
        else:
            pending_count += 1
    return ok_count, pending_count


def _replay_topology_svg(
    *,
    state_by_line: dict[str, list[str]],
    active_path: list[str],
    source_line: str,
    target_line: str,
    active_line: str,
    move_cars: list[str],
    train_cars: list[str],
    vehicle_target_tracks: dict[str, set[str]],
) -> str:
    layout = _replay_schematic_layout()
    tracks = layout["tracks"]
    width = float(layout["canvas"]["width"])
    height = float(layout["canvas"]["height"])
    mainline_tracks = {item.get("trackCode") for item in layout.get("mainlineTracks", [])}
    active_tracks = set(_replay_expand_path_for_map(active_path, tracks))
    active_tracks.update(item for item in [source_line, target_line, active_line] if item in tracks)
    move_set = set(move_cars) | set(train_cars)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height:.0f}" class="replay-topology-svg">',
        "<style>",
        ".replay-topology-svg{width:100%;height:auto;background:#f8fafc;border:1px solid #d9e2ec;border-radius:8px;}",
        ".replay-track-line{fill:none;stroke:#cbd5e1;stroke-width:10;stroke-linecap:round;stroke-linejoin:round;}",
        ".replay-track-mainline{stroke:#94a3b8;stroke-width:10;}",
        ".replay-track-source{stroke:#f97316;stroke-width:14;}",
        ".replay-track-target{stroke:#059669;stroke-width:14;}",
        ".replay-track-label{font-family:PingFang SC,Arial,sans-serif;font-size:24px;font-weight:750;fill:#1f2937;text-anchor:middle;}",
        ".replay-track-label-muted{font-size:18px;fill:#64748b;}",
        ".replay-track-label-active{font-size:27px;fill:#1d4ed8;}",
        ".replay-route-overlay{fill:none;stroke:#2563eb;stroke-width:14;stroke-linecap:round;stroke-linejoin:round;}",
        ".replay-badge-ok{fill:#0f766e;stroke:#ffffff;stroke-width:2;}",
        ".replay-badge-pending{fill:#d97706;stroke:#ffffff;stroke-width:2;}",
        ".replay-badge-active{stroke:#1f2937;stroke-width:3;}",
        ".replay-badge-text{font-family:Arial,sans-serif;font-size:14px;font-weight:850;fill:#ffffff;text-anchor:middle;dominant-baseline:middle;}",
        ".replay-endpoint-source{fill:#fff7ed;stroke:#f97316;stroke-width:3;}",
        ".replay-endpoint-target{fill:#ecfdf5;stroke:#059669;stroke-width:3;}",
        ".replay-endpoint-text{font-family:PingFang SC,Arial,sans-serif;font-size:18px;font-weight:850;text-anchor:middle;dominant-baseline:middle;}",
        "</style>",
        f'<rect x="0" y="0" width="{width:.0f}" height="{height:.0f}" fill="#f8fafc" rx="8" />',
    ]

    endpoint_track_overlays: list[str] = []
    badge_overlays: list[str] = []
    for line, track in tracks.items():
        points = track.get("points") or []
        if len(points) < 2:
            continue
        css_class = "replay-track-line"
        if line in mainline_tracks:
            css_class += " replay-track-mainline"
        parts.append(f'<path class="{css_class}" d="{_replay_points_path(points)}" />')
        if line == source_line:
            endpoint_track_overlays.append(f'<path class="replay-track-line replay-track-source" d="{_replay_points_path(points)}" />')
        if line == target_line:
            endpoint_track_overlays.append(f'<path class="replay-track-line replay-track-target" d="{_replay_points_path(points)}" />')
        label = track.get("labelAnchor") or points[len(points) // 2]
        label_class = "replay-track-label"
        if line not in active_tracks and not track.get("alwaysVisible") and not state_by_line.get(line):
            label_class += " replay-track-label-muted"
        if line in active_tracks:
            label_class += " replay-track-label-active"
        if line in active_tracks or track.get("alwaysVisible") or state_by_line.get(line):
            parts.append(
                f'<text class="{label_class}" x="{float(label[0]):.1f}" y="{float(label[1]):.1f}">{escape(line)}</text>'
            )
        cars = state_by_line.get(line, [])
        if cars:
            ok_count, pending_count = _replay_line_target_counts(line, cars, vehicle_target_tracks)
            active_badge = any(car in move_set for car in cars)
            badge_overlays.append(
                _replay_track_count_badges_svg(
                    track,
                    ok_count=ok_count,
                    pending_count=pending_count,
                    active=active_badge,
                )
            )

    for item in _replay_expand_path_for_map(active_path, tracks):
        track = tracks.get(item)
        if not track:
            continue
        points = track.get("points") or []
        if len(points) >= 2:
            parts.append(f'<path class="replay-route-overlay" d="{_replay_points_path(points)}" />')

    parts.extend(endpoint_track_overlays)
    parts.extend(badge_overlays)

    for line, css_class, label in [
        (source_line, "replay-endpoint-source", "取"),
        (target_line, "replay-endpoint-target", "放"),
    ]:
        track = tracks.get(line)
        if not track:
            continue
        cx, cy = _replay_track_center(track)
        parts.append(f'<circle class="{css_class}" cx="{cx:.1f}" cy="{cy:.1f}" r="15" />')
        parts.append(f'<text class="replay-endpoint-text" x="{cx:.1f}" y="{cy + 1:.1f}">{label}</text>')

    parts.append("</svg>")
    return "".join(parts)


def _replay_schematic_layout() -> dict:
    return json.loads(SCHEMATIC_LAYOUT_PATH.read_text(encoding="utf-8"))


def _replay_points_path(points: list[list[float]]) -> str:
    first = points[0]
    chunks = [f"M {float(first[0]):.1f} {float(first[1]):.1f}"]
    for point in points[1:]:
        chunks.append(f"L {float(point[0]):.1f} {float(point[1]):.1f}")
    return " ".join(chunks)


def _replay_track_center(track: dict) -> tuple[float, float]:
    points = track.get("points") or []
    if not points:
        return 0.0, 0.0
    return (
        sum(float(point[0]) for point in points) / len(points),
        sum(float(point[1]) for point in points) / len(points),
    )


def _replay_track_badge_anchor(track: dict) -> tuple[float, float]:
    label = track.get("labelAnchor")
    if label and len(label) >= 2:
        return float(label[0]) + 54.0, float(label[1]) - 18.0
    center_x, center_y = _replay_track_center(track)
    return center_x + 54.0, center_y - 18.0


def _replay_track_count_badges_svg(
    track: dict,
    *,
    ok_count: int,
    pending_count: int,
    active: bool,
) -> str:
    x, y = _replay_track_badge_anchor(track)
    active_class = " replay-badge-active" if active else ""
    parts: list[str] = []
    if ok_count > 0:
        parts.append(f'<circle class="replay-badge-ok{active_class}" cx="{x - 14:.1f}" cy="{y:.1f}" r="16" />')
        parts.append(f'<text class="replay-badge-text" x="{x - 14:.1f}" y="{y + 1:.1f}">{ok_count}</text>')
    if pending_count > 0:
        offset = 14 if ok_count > 0 else 0
        parts.append(f'<circle class="replay-badge-pending{active_class}" cx="{x + offset:.1f}" cy="{y:.1f}" r="16" />')
        parts.append(f'<text class="replay-badge-text" x="{x + offset:.1f}" y="{y + 1:.1f}">{pending_count}</text>')
    return "".join(parts)


def _replay_expand_path_for_map(active_path: list[str], tracks: dict) -> list[str]:
    endpoint_connectors = {
        "修1库内": ["修1库外", "修1库内"],
        "修1库外": ["修1库外"],
        "修2库内": ["修2库外", "修2库内"],
        "修2库外": ["修2库外"],
        "修3库内": ["修3库外", "修3库内"],
        "修3库外": ["修3库外"],
        "修4库内": ["修4库外", "修4库内"],
        "修4库外": ["修4库外"],
        "调梁棚": ["调梁线北", "调梁棚"],
        "机库线": ["机库线"],
        "机走棚": ["机走北", "机走棚"],
        "预修线": ["预修线"],
        "洗罐站": ["洗罐线北", "洗罐站"],
        "油漆线": ["油漆线"],
        "抛丸线": ["抛丸线"],
        "卸轮线": ["卸轮线"],
    }
    expanded: list[str] = []
    for index, item in enumerate(active_path):
        mapped = endpoint_connectors.get(item, [item] if item in tracks else [])
        for track in mapped:
            if track in tracks and (not expanded or expanded[-1] != track):
                expanded.append(track)
    return expanded


def _replay_yard_svg(
    *,
    state_by_line: dict[str, list[str]],
    active_path: set[str],
    source_line: str,
    target_line: str,
    move_cars: list[str],
    train_cars: list[str],
    vehicle_display_labels: dict[str, str],
) -> str:
    groups = _replay_line_groups(state_by_line, active_path | {source_line, target_line})
    width = 1220
    margin = 24
    gap = 18
    column_count = len(groups)
    column_width = (width - margin * 2 - gap * (column_count - 1)) / column_count
    row_height = 44
    max_rows = max((len(lines) for _, lines in groups), default=1)
    height = 78 + max_rows * row_height + 24
    move_set = set(move_cars) | set(train_cars)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" class="replay-yard-svg">',
        "<style>",
        ".replay-yard-svg{width:100%;height:auto;background:#f8fafc;border:1px solid #d9e2ec;border-radius:8px;}",
        ".replay-yard-title{font-family:PingFang SC,Arial,sans-serif;font-size:15px;font-weight:700;fill:#1f2937;}",
        ".replay-line-label{font-family:PingFang SC,Arial,sans-serif;font-size:12px;font-weight:700;fill:#334155;}",
        ".replay-line-count{font-family:Arial,sans-serif;font-size:11px;font-weight:700;fill:#475569;text-anchor:end;}",
        ".replay-track{fill:#ffffff;stroke:#cbd5e1;stroke-width:1.4;}",
        ".replay-track-path{fill:#eff6ff;stroke:#2563eb;stroke-width:2.4;}",
        ".replay-track-source{fill:#fff7ed;stroke:#f97316;stroke-width:2.8;}",
        ".replay-track-target{fill:#ecfdf5;stroke:#059669;stroke-width:2.8;}",
        ".replay-chip{fill:#e2e8f0;stroke:#ffffff;stroke-width:1;}",
        ".replay-chip-active{fill:#0f766e;}",
        ".replay-chip-text{font-family:Arial,sans-serif;font-size:9px;font-weight:700;fill:#334155;text-anchor:middle;dominant-baseline:middle;}",
        ".replay-chip-text-active{fill:#ffffff;}",
        ".replay-more{font-family:Arial,sans-serif;font-size:10px;font-weight:700;fill:#64748b;}",
        "</style>",
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#f8fafc" rx="8" />',
    ]

    for group_index, (group_name, lines) in enumerate(groups):
        x = margin + group_index * (column_width + gap)
        parts.append(
            f'<text class="replay-yard-title" x="{x:.1f}" y="34">{escape(group_name)}</text>'
        )
        for row_index, line in enumerate(lines):
            y = 62 + row_index * row_height
            cars = state_by_line.get(line, [])
            track_x = x + 76
            track_y = y - 17
            track_width = column_width - 86
            css_class = "replay-track"
            if line in active_path:
                css_class = "replay-track replay-track-path"
            if line == source_line:
                css_class = "replay-track replay-track-source"
            if line == target_line:
                css_class = "replay-track replay-track-target"
            parts.append(f'<text class="replay-line-label" x="{x:.1f}" y="{y + 4:.1f}">{escape(line)}</text>')
            parts.append(
                f'<rect class="{css_class}" x="{track_x:.1f}" y="{track_y:.1f}" '
                f'width="{track_width:.1f}" height="24" rx="5" />'
            )
            parts.append(
                f'<text class="replay-line-count" x="{track_x + track_width - 7:.1f}" '
                f'y="{y + 3:.1f}">{len(cars)}</text>'
            )
            chip_x = track_x + 7
            chip_width = 92
            chip_gap = 4
            max_chips = max(1, int((track_width - 48) // (chip_width + chip_gap)))
            for chip_index, car_no in enumerate(cars[:max_chips]):
                cx = chip_x + chip_index * (chip_width + chip_gap)
                is_active = car_no in move_set
                chip_class = "replay-chip replay-chip-active" if is_active else "replay-chip"
                text_class = "replay-chip-text replay-chip-text-active" if is_active else "replay-chip-text"
                full_label = _replay_vehicle_label(car_no, vehicle_display_labels)
                compact_label = _replay_compact_vehicle_label(car_no, vehicle_display_labels)
                parts.append("<g>")
                parts.append(f"<title>{escape(full_label)}</title>")
                parts.append(
                    f'<rect class="{chip_class}" x="{cx:.1f}" y="{track_y + 4:.1f}" '
                    f'width="{chip_width}" height="16" rx="4" />'
                )
                parts.append(
                    f'<text class="{text_class}" x="{cx + chip_width / 2:.1f}" y="{track_y + 12.5:.1f}">'
                    f"{escape(compact_label)}</text>"
                )
                parts.append("</g>")
            if len(cars) > max_chips:
                parts.append(
                    f'<text class="replay-more" x="{chip_x + max_chips * (chip_width + chip_gap) + 2:.1f}" '
                    f'y="{track_y + 16:.1f}">+{len(cars) - max_chips}</text>'
                )

    parts.append("</svg>")
    return "".join(parts)


def _replay_line_groups(state_by_line: dict[str, list[str]], active_lines: set[str]) -> list[tuple[str, list[str]]]:
    storage = ["存5线北", "存5线南", "存4线", "存4南", "存3线", "存2线", "存1线", "调梁线北", "机走北", "洗罐线北"]
    operation = ["调梁棚", "机走棚", "预修线", "洗罐站", "抛丸线", "油漆线", "卸轮线"]
    depot = ["修1库内", "修1库外", "修2库内", "修2库外", "修3库内", "修3库外", "修4库内", "修4库外"]
    loco_temp = ["机库线", "机北1", "机北2", "机南", "洗油北"]
    known = set(storage) | set(operation) | set(depot) | set(loco_temp)
    extra = sorted(
        line
        for line in set(state_by_line) | set(active_lines)
        if line and line not in known and not re.fullmatch(r"[LZ]\d+", line)
    )
    return [
        ("存车区", storage),
        ("功能/预修", operation),
        ("大库", depot),
        ("机务/临时", loco_temp + extra),
    ]


def _replay_route_chips_html(path: list[str]) -> str:
    chips = path or ["无路径"]
    chip_html = "".join(f"<span class='replay-route-chip'>{escape(item)}</span>" for item in chips)
    return f"""
    <style>
    .replay-route-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin: 8px 0 12px 0;
    }}
    .replay-route-chip {{
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 3px 8px;
      border-radius: 6px;
      background: #e0f2fe;
      color: #075985;
      border: 1px solid #bae6fd;
      font-size: 12px;
      font-weight: 650;
      line-height: 1.2;
    }}
    </style>
    <div class="replay-route-row">{chip_html}</div>
    """


def _replay_cars_table_html(rows: list[dict[str, object]]) -> str:
    if not rows:
        return ""
    columns = [column for column in ("line", "carCount", "cars") if column in rows[0]]
    header_labels = {
        "line": "股道",
        "carCount": "辆数",
        "cars": "车辆",
    }
    header_html = "".join(
        f"<th class='replay-cars-col-{escape(column)}'>{escape(header_labels.get(column, column))}</th>"
        for column in columns
    )
    body_rows = []
    for row in rows:
        cells = []
        for column in columns:
            value = row.get(column, "")
            if column == "cars":
                value_html = _replay_cars_cell_html(value)
            else:
                value_html = escape(str(value))
            cells.append(f"<td class='replay-cars-col-{escape(column)}'>{value_html}</td>")
        body_rows.append(f"<tr>{''.join(cells)}</tr>")
    return f"""
    <style>
    .replay-cars-table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      margin: 8px 0 14px 0;
      border: 1px solid #d9e2ec;
      border-radius: 8px;
      overflow: hidden;
      font-size: 13px;
    }}
    .replay-cars-table th,
    .replay-cars-table td {{
      border-bottom: 1px solid #e5edf5;
      padding: 7px 9px;
      vertical-align: top;
      line-height: 1.35;
    }}
    .replay-cars-table th {{
      background: #f8fafc;
      color: #475569;
      font-weight: 700;
      text-align: left;
    }}
    .replay-cars-table tr:last-child td {{
      border-bottom: 0;
    }}
    .replay-cars-col-line {{
      width: 112px;
      white-space: nowrap;
    }}
    .replay-cars-col-carCount {{
      width: 64px;
      text-align: right;
      white-space: nowrap;
    }}
    .replay-cars-col-cars {{
      width: auto;
      overflow-wrap: anywhere;
    }}
    .replay-cars-list {{
      display: flex;
      flex-wrap: wrap;
      gap: 5px 8px;
      align-items: flex-start;
    }}
    .replay-cars-item {{
      display: inline-block;
      padding: 2px 6px;
      border-radius: 5px;
      background: #f1f5f9;
      color: #0f172a;
      border: 1px solid #e2e8f0;
      overflow-wrap: anywhere;
    }}
    </style>
    <table class="replay-cars-table">
      <thead><tr>{header_html}</tr></thead>
      <tbody>{''.join(body_rows)}</tbody>
    </table>
    """


def _replay_cars_cell_html(value) -> str:
    if isinstance(value, (list, tuple)):
        items = [str(item) for item in value if str(item)]
    else:
        items = _replay_split_vehicle_labels(str(value))
    if not items:
        return ""
    return "<div class='replay-cars-list'>" + "".join(
        f"<span class='replay-cars-item'>{escape(item)}</span>"
        for item in items
    ) + "</div>"


def _replay_split_vehicle_labels(value: str) -> list[str]:
    text = value.strip()
    if not text:
        return []
    matches = re.findall(r"\S+\([^)]*\)", text)
    return matches or [text]


def _replay_state_table_rows(
    state_by_line: dict[str, list[str]],
    *,
    highlighted_lines: set[str],
    vehicle_display_labels: dict[str, str],
) -> list[dict[str, object]]:
    rows = []
    for line in sorted(state_by_line):
        cars = state_by_line.get(line, [])
        if not cars and line not in highlighted_lines:
            continue
        rows.append(
            {
                "line": line,
                "highlighted": line in highlighted_lines,
                "carCount": len(cars),
                "cars": [
                    _replay_vehicle_label(car_no, vehicle_display_labels)
                    for car_no in cars
                ],
            }
        )
    return rows


def _render_replay_end_status(response: dict, vehicle_display_labels: dict[str, str]) -> None:
    rows = _replay_end_status_rows(response, vehicle_display_labels)
    if not rows:
        st.info("接口响应中没有 GeneratedEndStatus。")
        return
    line_rows = []
    grouped: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        grouped[str(row["line"])].append(str(row["vehicleNo"]))
    for line, cars in sorted(grouped.items()):
        line_rows.append({"line": line, "carCount": len(cars), "cars": cars})
    st.markdown("**终态股道汇总**")
    st.markdown(_replay_cars_table_html(line_rows), unsafe_allow_html=True)
    st.markdown("**终态车辆位置**")
    st.dataframe(rows, width="stretch", hide_index=True)


def _replay_end_status_rows(response: dict, vehicle_display_labels: dict[str, str]) -> list[dict[str, object]]:
    status_rows = ((response or {}).get("Data") or {}).get("GeneratedEndStatus") or []
    rows = [
        {
            "vehicleNo": _replay_vehicle_label(item.get("No"), vehicle_display_labels),
            "line": str(item.get("Line") or ""),
            "position": _replay_int_or_zero(item.get("Position")),
        }
        for item in status_rows
    ]
    return sorted(rows, key=lambda row: (row["line"], row["position"], row["vehicleNo"]))



def _replay_split_pipe(text: str) -> list[str]:
    return [item for item in str(text or "").split("|") if item]


def _replay_extend_route(route: list[str], segment: list[str]) -> list[str]:
    if not segment:
        return route
    if not route:
        return list(segment)
    result = list(route)
    for item in segment:
        if result and result[-1] == item:
            continue
        result.append(item)
    return result


def _replay_int_or_zero(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _stage_simple_output_dir(stage_no: int) -> Path:
    return STAGE_SIMPLE_OUTPUT_DIRS[stage_no]


def _stage_path_for_command(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT_DIR))
    except ValueError:
        return str(path)


def _stage_simple_run_command(stage_no: int) -> str:
    input_path = _stage_path_for_command(TRUTH2_DIR)
    out_path = _stage_path_for_command(_stage_simple_output_dir(stage_no))
    if stage_no == 1:
        return f"python3 scripts/stage1_simple/solve.py {input_path} --out {out_path}"
    if stage_no == 2:
        stage1_out = _stage_path_for_command(_stage_simple_output_dir(1))
        return (
            f"python3 scripts/stage2_simple/solve.py {input_path} "
            f"--stage1-out {stage1_out} --out {out_path}"
        )
    if stage_no == 3:
        stage2_out = _stage_path_for_command(_stage_simple_output_dir(2))
        return (
            f"python3 scripts/stage3_simple/solve.py {input_path} "
            f"--stage2-out {stage2_out} --out {out_path}"
        )
    if stage_no == 4:
        stage3_out = _stage_path_for_command(_stage_simple_output_dir(3))
        return (
            f"python3 scripts/stage4_simple/solve.py {input_path} "
            f"--stage3-out {stage3_out} --out {out_path}"
        )
    raise ValueError(f"unsupported stage: {stage_no}")


def _stage_simple_run_commands(*stage_nos: int) -> str:
    return "\n".join(_stage_simple_run_command(stage_no) for stage_no in stage_nos)


def _render_stage_simple_io_paths(stage_no: int, output_path: Path) -> None:
    rows = [{"类型": "输入 JSON", "路径": _stage_path_for_command(TRUTH2_DIR)}]
    if stage_no > 1:
        rows.append(
            {
                "类型": f"上游 Stage{stage_no - 1} 输出",
                "路径": _stage_path_for_command(_stage_simple_output_dir(stage_no - 1)),
            }
        )
    rows.append({"类型": f"Stage{stage_no} 输出", "路径": _stage_path_for_command(output_path)})
    with st.expander("输入/输出路径", expanded=False):
        st.dataframe(rows, width="stretch", hide_index=True)
        st.code(_stage_simple_run_command(stage_no), language="bash")


_FULLFLOW_STAGE_LABELS = {
    "stage1": "第一阶段",
    "stage2": "第二阶段",
    "stage3": "第三阶段",
    "stage4": "第四阶段",
}


def _fullflow_case_rows(artifact_root: Path, dataset: str) -> list[dict[str, object]]:
    dataset_root = artifact_root / dataset
    case_ids = sorted(
        path.name[: -len("_summary.json")]
        for path in (dataset_root / "stage1").glob("*_summary.json")
        if path.name != "aggregate_summary.json"
    )
    rows: list[dict[str, object]] = []
    for case_id in case_ids:
        summaries = {
            stage: _read_json(dataset_root / stage / f"{case_id}_summary.json")
            for stage in _FULLFLOW_STAGE_LABELS
            if (dataset_root / stage / f"{case_id}_summary.json").exists()
        }
        responses = {
            stage: _read_json(dataset_root / stage / f"{case_id}_response.json")
            for stage in _FULLFLOW_STAGE_LABELS
            if (dataset_root / stage / f"{case_id}_response.json").exists()
        }
        statuses = {
            stage: str((summaries.get(stage) or {}).get("status") or "missing")
            for stage in _FULLFLOW_STAGE_LABELS
        }
        stage_hooks = {
            stage: _business_hook_count(responses.get(stage) or {})
            for stage in _FULLFLOW_STAGE_LABELS
        }
        combined_path = dataset_root / "stage4" / f"{case_id}_combined_response.json"
        combined_response = _read_json(combined_path) if combined_path.exists() else {}
        full_status = "complete" if all(status == "complete" for status in statuses.values()) else "partial"
        failed_stage = next(
            (
                _FULLFLOW_STAGE_LABELS[stage]
                for stage, status in statuses.items()
                if status != "complete"
            ),
            "",
        )
        stage4_summary = summaries.get("stage4") or {}
        rows.append(
            {
                "caseId": case_id,
                "status": full_status,
                "failedStage": failed_stage,
                "stage1": statuses["stage1"],
                "stage2": statuses["stage2"],
                "stage3": statuses["stage3"],
                "stage4": statuses["stage4"],
                "stage1Hooks": stage_hooks["stage1"],
                "stage2Hooks": stage_hooks["stage2"],
                "stage3Hooks": stage_hooks["stage3"],
                "stage4Hooks": stage_hooks["stage4"],
                "businessHooks": (
                    _business_hook_count(combined_response)
                    if combined_response
                    else sum(stage_hooks.values())
                ),
                "finalUnsatisfied": int(stage4_summary.get("final_unsatisfied_count") or 0),
                "combinedReplayOk": stage4_summary.get("combined_replay_physical_ok"),
                "blockingReasons": " | ".join(stage4_summary.get("blocking_reasons") or []),
            }
        )
    return sorted(rows, key=lambda row: (row["status"] != "complete", str(row["caseId"])))


def _fullflow_stage_boundaries(stage_responses: dict[str, dict]) -> tuple[list[dict[str, object]], list[str]]:
    rows: list[dict[str, object]] = []
    stage_sequence: list[str] = []
    operation_offset = 0
    hook_offset = 0
    for stage, label in _FULLFLOW_STAGE_LABELS.items():
        response = stage_responses.get(stage) or {}
        operations = ((response.get("Data") or {}).get("Operations") or [])
        operation_count = len(operations)
        business_hook_count = _business_hook_count(response)
        operation_start = operation_offset + 1 if operation_count else 0
        operation_end = operation_offset + operation_count
        hook_start = hook_offset + 1 if business_hook_count else 0
        hook_end = hook_offset + business_hook_count
        rows.append(
            {
                "stage": label,
                "statusOperations": operation_count,
                "operationRange": (
                    f"{operation_start}-{operation_end}" if operation_count else "无"
                ),
                "businessHooks": business_hook_count,
                "businessHookRange": f"{hook_start}-{hook_end}" if business_hook_count else "无",
            }
        )
        stage_sequence.extend([label] * operation_count)
        operation_offset = operation_end
        hook_offset = hook_end
    return rows, stage_sequence


def _fullflow_operation_table_rows(
    operation_rows,
    vehicle_display_labels: dict[str, str],
    stage_sequence: list[str],
) -> list[dict[str, object]]:
    rows = _replay_operation_table_rows(operation_rows, vehicle_display_labels)
    return [
        {"stage": stage_sequence[index] if index < len(stage_sequence) else "未知阶段", **row}
        for index, row in enumerate(rows)
    ]


def _render_fullflow_replay_dashboard() -> None:
    st.subheader("调车全流程回放")
    st.caption("选择一个案例，从原始入段状态连续回放第一阶段至第四阶段的全部调车操作。")
    artifact_text = st.text_input(
        "全流程结果根目录",
        value=str(DEFAULT_FULLFLOW_ARTIFACT_DIR),
        key="fullflow-artifact-root",
    )
    artifact_root = Path(artifact_text).expanduser()
    datasets = [
        dataset
        for dataset in ("truth2", "truth3")
        if (artifact_root / dataset / "stage1").exists()
        and (artifact_root / dataset / "stage4").exists()
    ]
    if not datasets:
        st.warning("结果根目录中没有找到 truth2/truth3 的 stage1 至 stage4 输出。")
        return
    dataset = st.radio(
        "数据集",
        options=datasets,
        horizontal=True,
        key="fullflow-dataset",
    )
    dataset_root = artifact_root / dataset
    case_rows = _fullflow_case_rows(artifact_root, dataset)
    if not case_rows:
        st.warning(f"{dataset} 中没有可用案例。")
        return

    complete_rows = [row for row in case_rows if row["status"] == "complete"]
    complete_hooks = [int(row["businessHooks"] or 0) for row in complete_rows]
    metrics = st.columns(5)
    metrics[0].metric("案例数", len(case_rows))
    metrics[1].metric("全流程完成", len(complete_rows))
    metrics[2].metric("Partial", len(case_rows) - len(complete_rows))
    metrics[3].metric("完成均勾", _average(complete_hooks))
    metrics[4].metric("完成最大勾", max(complete_hooks) if complete_hooks else 0)

    filters = st.columns([2, 2, 3])
    status_filter = filters[0].selectbox(
        "状态",
        ["全部", "complete", "partial"],
        key="fullflow-status-filter",
    )
    min_hooks = filters[1].number_input(
        "最小总勾数",
        min_value=0,
        value=0,
        step=1,
        key="fullflow-min-hooks",
    )
    query = filters[2].text_input(
        "案例/失败阶段/阻塞原因搜索",
        value="",
        key="fullflow-case-query",
    )
    filtered_rows = _filter_case_rows(
        case_rows,
        status_filter=status_filter,
        min_hooks=int(min_hooks),
        query=query,
    )
    st.dataframe(filtered_rows, width="stretch", hide_index=True)
    if not filtered_rows:
        return

    selected_case = st.selectbox(
        "选中案例",
        options=[str(row["caseId"]) for row in filtered_rows],
        format_func=lambda case_id: next(
            (
                f"{case_id} | {row['status']} | {row['businessHooks']} 勾"
                f"{(' | 失败于' + str(row['failedStage'])) if row['failedStage'] else ''}"
                for row in filtered_rows
                if row["caseId"] == case_id
            ),
            case_id,
        ),
        key="fullflow-selected-case",
    )
    selected_row = next(row for row in case_rows if row["caseId"] == selected_case)
    stage_summaries = {
        stage: _read_json(dataset_root / stage / f"{selected_case}_summary.json")
        for stage in _FULLFLOW_STAGE_LABELS
        if (dataset_root / stage / f"{selected_case}_summary.json").exists()
    }
    stage_responses = {
        stage: _read_json(dataset_root / stage / f"{selected_case}_response.json")
        for stage in _FULLFLOW_STAGE_LABELS
        if (dataset_root / stage / f"{selected_case}_response.json").exists()
    }
    combined_path = dataset_root / "stage4" / f"{selected_case}_combined_response.json"
    combined_response = _read_json(combined_path) if combined_path.exists() else {}
    request_payload = _load_truth_payload(selected_case)

    selected_metrics = st.columns(7)
    selected_metrics[0].metric("全流程状态", selected_row["status"])
    selected_metrics[1].metric("总业务勾", selected_row["businessHooks"])
    selected_metrics[2].metric("第一阶段", selected_row["stage1Hooks"])
    selected_metrics[3].metric("第二阶段", selected_row["stage2Hooks"])
    selected_metrics[4].metric("第三阶段", selected_row["stage3Hooks"])
    selected_metrics[5].metric("第四阶段", selected_row["stage4Hooks"])
    selected_metrics[6].metric("CombinedReplay", _stage_yes_no(selected_row["combinedReplayOk"]))
    st.caption(
        "阶段状态："
        + " | ".join(
            f"{label}={selected_row[stage]}"
            for stage, label in _FULLFLOW_STAGE_LABELS.items()
        )
    )
    if selected_row["blockingReasons"]:
        st.info("阻塞原因：" + str(selected_row["blockingReasons"]))

    boundary_rows, stage_sequence = _fullflow_stage_boundaries(stage_responses)
    st.markdown("**阶段边界**")
    st.dataframe(boundary_rows, width="stretch", hide_index=True)

    view = st.radio(
        "查看内容",
        options=["可视化回放", "全流程勾计划", "阶段摘要", "终态", "原始 JSON"],
        horizontal=True,
        key="fullflow-view",
    )
    if view == "可视化回放":
        if not request_payload:
            st.warning(f"没有找到案例 {selected_case} 的原始 truth 请求。")
        elif not combined_response:
            st.warning("该案例没有 Stage4 combined_response，只能在对应阶段页签查看已完成片段。")
        else:
            operation_rows = _response_operation_rows(combined_response)
            if len(operation_rows) != len(stage_sequence):
                st.warning(
                    f"阶段片段共 {len(stage_sequence)} 条操作，但 combined_response 有 "
                    f"{len(operation_rows)} 条；阶段标签可能不完整。"
                )
            operation_stage_labels = {
                row.operation_index: (
                    stage_sequence[index] if index < len(stage_sequence) else "未知阶段"
                )
                for index, row in enumerate(operation_rows)
            }
            display_response = _stage_response_with_generated(request_payload, combined_response)
            vehicle_labels = _replay_vehicle_display_labels(request_payload)
            _render_replay_replay(
                request_payload,
                operation_rows,
                display_response,
                vehicle_labels,
                key_prefix=f"fullflow-{dataset}",
                operation_stage_labels=operation_stage_labels,
            )
    elif view == "全流程勾计划":
        if not combined_response:
            st.warning("该案例没有 Stage4 combined_response。")
        else:
            operation_rows = _response_operation_rows(combined_response)
            vehicle_labels = _replay_vehicle_display_labels(request_payload or {})
            st.dataframe(
                _fullflow_operation_table_rows(operation_rows, vehicle_labels, stage_sequence),
                width="stretch",
                hide_index=True,
            )
    elif view == "阶段摘要":
        st.dataframe(
            [
                {
                    "stage": label,
                    "status": (stage_summaries.get(stage) or {}).get("status", "missing"),
                    "businessHooks": _business_hook_count(stage_responses.get(stage) or {}),
                    "blockingReasons": " | ".join(
                        (stage_summaries.get(stage) or {}).get("blocking_reasons") or []
                    ),
                }
                for stage, label in _FULLFLOW_STAGE_LABELS.items()
            ],
            width="stretch",
            hide_index=True,
        )
    elif view == "终态":
        if combined_response:
            _render_replay_end_status(
                combined_response,
                _replay_vehicle_display_labels(request_payload or {}),
            )
        else:
            st.info("当前没有全流程终态。")
    else:
        left, right = st.columns(2)
        with left:
            st.markdown("**原始请求**")
            st.json(request_payload or {})
            st.markdown("**阶段 summaries**")
            st.json(stage_summaries)
        with right:
            st.markdown("**Stage4 combined_response**")
            st.json(combined_response)


def _render_stage1_simple_dashboard() -> None:
    st.subheader("第一阶段可视化")
    st.caption("读取 scripts/stage1_simple 的输出，查看全量完成情况、单案例勾计划、线路回放和终态边界。")
    artifact_text = st.text_input(
        "第一阶段输出目录",
        value=str(_stage_simple_output_dir(1)),
        key="stage1-simple-artifact-dir",
    )
    artifact_dir = Path(artifact_text).expanduser()
    _render_stage_simple_io_paths(1, artifact_dir)
    aggregate_path = artifact_dir / "aggregate_summary.json"
    if not aggregate_path.exists():
        st.warning("没有找到 aggregate_summary.json。请先运行 stage1_simple 求解器生成输出。")
        st.code(_stage_simple_run_command(1), language="bash")
        return

    try:
        aggregate = _read_json(aggregate_path)
    except Exception as exc:  # noqa: BLE001
        st.error(f"读取 aggregate_summary.json 失败：{exc}")
        return

    summaries = list(aggregate.get("summaries") or [])
    if not summaries:
        st.warning("aggregate_summary.json 中没有 summaries。")
        return

    case_rows = _stage1_case_rows(summaries, artifact_dir)
    business_hook_values = [int(row.get("businessHooks") or 0) for row in case_rows]
    move_batch_values = [int(row.get("moveBatches") or 0) for row in case_rows]
    over_40 = [row for row in case_rows if int(row.get("businessHooks") or 0) > 40]
    metric_cols = st.columns(8)
    metric_cols[0].metric("案例数", aggregate.get("cases", len(summaries)))
    metric_cols[1].metric("完成", aggregate.get("complete", "-"))
    metric_cols[2].metric("Partial", aggregate.get("partial", "-"))
    metric_cols[3].metric("Error", aggregate.get("error", 0))
    metric_cols[4].metric("平均业务勾", _average(business_hook_values))
    metric_cols[5].metric("最大业务勾", max(business_hook_values) if business_hook_values else 0)
    metric_cols[6].metric(">40 业务勾", len(over_40))
    metric_cols[7].metric("平均搬运批次", _average(move_batch_values))
    st.caption(
        "口径说明：现场业务勾数 = Get/Put 次数；搬运批次 = 求解器一次 Get+Put 搬运，"
        "通常等于 2 个业务勾。称重 Weigh 不计入业务勾。"
    )

    filter_cols = st.columns([2, 2, 3])
    status_filter = filter_cols[0].selectbox(
        "状态",
        ["全部", "complete", "partial", "error"],
        key="stage1-status-filter",
    )
    min_hooks = filter_cols[1].number_input("最小业务勾数", min_value=0, value=0, step=1, key="stage1-min-hooks")
    case_query = filter_cols[2].text_input("案例/阻塞原因搜索", value="", key="stage1-case-query")
    filtered_rows = _filter_case_rows(
        case_rows,
        status_filter=status_filter,
        min_hooks=int(min_hooks),
        query=case_query,
    )
    st.markdown("**全量案例**")
    st.caption(f"当前显示 {len(filtered_rows)} / {len(case_rows)} 个案例。")
    st.dataframe(filtered_rows, width="stretch", hide_index=True)

    if not filtered_rows:
        return
    selected_case = st.selectbox(
        "选中案例",
        options=[str(row["caseId"]) for row in filtered_rows],
        format_func=lambda case_id: _stage1_case_label(case_id, filtered_rows),
        key="stage1-selected-case",
    )
    bundle = _stage1_load_case_bundle(artifact_dir, selected_case)
    if not bundle:
        st.warning(f"案例 {selected_case} 的 response/summary/trace 文件不完整。")
        return

    summary = bundle["summary"]
    response = bundle["response"]
    trace = bundle["trace"]
    request_payload = _load_truth_payload(selected_case)
    if request_payload is None:
        st.warning(f"没有在 data/truth2 中找到 {selected_case} 的原始输入，无法做车辆标签和初始回放。")
        request_payload = {"StartStatus": [], "locoNode": {}}

    selected_cols = st.columns(6)
    debt = summary.get("stage1_debt") or {}
    business_hook_count = _business_hook_count(response)
    weigh_count = _stage1_action_count(response, "Weigh")
    selected_cols[0].metric("状态", summary.get("status", ""))
    selected_cols[1].metric("业务勾数", business_hook_count)
    selected_cols[2].metric("搬运批次", summary.get("hooks", 0))
    selected_cols[3].metric("债务", debt.get("debt_count", 0))
    selected_cols[4].metric("待编组", len(debt.get("pending_stage1_nos") or []))
    selected_cols[5].metric("称重操作", weigh_count)

    operation_rows = _response_operation_rows(response)
    vehicle_display_labels = _replay_vehicle_display_labels(request_payload)
    view = st.radio(
        "查看内容",
        options=["可视化回放", "勾计划", "终态", "Trace/诊断", "原始 JSON"],
        horizontal=True,
        key="stage1-view",
    )
    if view == "可视化回放":
        _render_replay_replay(request_payload, operation_rows, response, vehicle_display_labels, key_prefix="stage1")
    elif view == "勾计划":
        hook_rows = _replay_hook_summary_rows(operation_rows, vehicle_display_labels)
        st.markdown("**按搬运批次汇总**")
        st.caption("每个搬运批次通常包含 1 次 Get + 1 次 Put，即 2 个现场业务勾；有称重时中间多 1 次 Weigh。")
        st.dataframe(hook_rows, width="stretch", hide_index=True)
        st.markdown("**接口操作序列（Get/Put 为业务勾）**")
        st.dataframe(_replay_operation_table_rows(operation_rows, vehicle_display_labels), width="stretch", hide_index=True)
    elif view == "终态":
        _render_replay_end_status(response, vehicle_display_labels)
    elif view == "Trace/诊断":
        _render_stage1_trace(trace, summary, vehicle_display_labels)
    else:
        json_cols = st.columns(2)
        with json_cols[0]:
            st.markdown("**summary**")
            st.json(summary)
            st.markdown("**trace**")
            st.json(trace)
        with json_cols[1]:
            st.markdown("**response**")
            st.json(_replay_response_for_display(response, vehicle_display_labels))


def _average(values: list[int]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


def _stage1_case_rows(summaries: list[dict], artifact_dir: Path | None = None) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for summary in summaries:
        debt = summary.get("stage1_debt") or {}
        case_id = str(summary.get("case_id") or "")
        response = _try_load_response(artifact_dir, case_id) if artifact_dir is not None else {}
        business_hooks = _business_hook_count(response) if response else int(summary.get("operations") or 0)
        weigh_ops = _stage1_action_count(response, "Weigh") if response else 0
        rows.append(
            {
                "caseId": case_id,
                "status": summary.get("status", ""),
                "businessHooks": business_hooks,
                "moveBatches": int(summary.get("hooks") or 0),
                "interfaceOperations": int(summary.get("operations") or 0),
                "weighOps": weigh_ops,
                "debt": int(debt.get("debt_count") or 0),
                "pending": len(debt.get("pending_stage1_nos") or []),
                "pollution": len(debt.get("pollution_nos") or []),
                "blockedG": int(debt.get("blocked_g_count") or 0),
                "blockingReasons": " | ".join(summary.get("blocking_reasons") or []),
            }
        )
    return sorted(rows, key=lambda row: (-int(row["businessHooks"]), str(row["caseId"])))


def _filter_case_rows(
    rows: list[dict[str, object]],
    *,
    status_filter: str,
    min_hooks: int,
    query: str,
) -> list[dict[str, object]]:
    query = query.strip().lower()
    result: list[dict[str, object]] = []
    for row in rows:
        if status_filter != "全部" and row.get("status") != status_filter:
            continue
        if int(row.get("businessHooks") or 0) < min_hooks:
            continue
        haystack = " ".join(str(value) for value in row.values()).lower()
        if query and query not in haystack:
            continue
        result.append(row)
    return result


def _stage1_case_label(case_id: str, rows: list[dict]) -> str:
    row = next((item for item in rows if item.get("caseId") == case_id), {})
    return (
        f"{case_id} | {row.get('status', '')} | "
        f"{row.get('businessHooks', 0)} 业务勾 / {row.get('moveBatches', 0)} 搬运批次"
    )


def _try_load_response(artifact_dir: Path | None, case_id: str) -> dict:
    if artifact_dir is None or not case_id:
        return {}
    path = artifact_dir / f"{case_id}_response.json"
    if not path.exists():
        return {}
    try:
        return _read_json(path)
    except Exception:  # noqa: BLE001
        return {}


def _business_hook_count(response: dict) -> int:
    return sum(
        1
        for op in (((response or {}).get("Data") or {}).get("Operations") or [])
        if op.get("Action") in BUSINESS_HOOK_ACTIONS
    )


def _stage1_action_count(response: dict, action: str) -> int:
    return sum(
        1
        for op in (((response or {}).get("Data") or {}).get("Operations") or [])
        if op.get("Action") == action
    )


def _stage1_load_case_bundle(artifact_dir: Path, case_id: str) -> dict[str, object] | None:
    paths = {
        "summary": artifact_dir / f"{case_id}_summary.json",
        "response": artifact_dir / f"{case_id}_response.json",
        "trace": artifact_dir / f"{case_id}_trace.json",
    }
    if not all(path.exists() for path in paths.values()):
        return None
    return {
        key: _read_json(path)
        for key, path in paths.items()
    }


def _load_truth_payload(case_id: str) -> dict | None:
    matches = sorted(
        path
        for truth_dir in (TRUTH2_DIR, TRUTH3_DIR)
        for path in truth_dir.glob(f"*{case_id}.json")
    )
    if not matches:
        return None
    try:
        return _read_json_object(matches[0])
    except Exception:  # noqa: BLE001
        return None


def _response_operation_rows(response: dict) -> list[ReplayOperationRow]:
    operations = ((response or {}).get("Data") or {}).get("Operations") or []
    rows: list[ReplayOperationRow] = []
    hook_index = 0
    for sequence_index, op in enumerate(operations, start=1):
        action = str(op.get("Action") or "")
        if action == "Get":
            hook_index += 1
        elif hook_index <= 0:
            hook_index = 1
        rows.append(
            ReplayOperationRow(
                hook_index=hook_index,
                operation_index=int(op.get("Index") or sequence_index),
                action=action,
                line=str(op.get("Line") or ""),
                move_cars=_operation_value_to_pipe(op.get("MoveCars")),
                train_cars=_operation_value_to_pipe(op.get("TrainCars")),
                passby_path=_operation_value_to_pipe(op.get("PassbyPath")),
            )
        )
    return rows


def _manual_response_operation_rows(response: dict) -> list[ReplayOperationRow]:
    operations = ((response or {}).get("Data") or {}).get("Operations") or []
    rows: list[ReplayOperationRow] = []
    for sequence_index, op in enumerate(operations, start=1):
        operation_index = int(op.get("Index") or sequence_index)
        rows.append(
            ReplayOperationRow(
                hook_index=int(op.get("ManualHook") or operation_index),
                operation_index=operation_index,
                action=str(op.get("Action") or ""),
                line=str(op.get("Line") or ""),
                move_cars=_operation_value_to_pipe(op.get("MoveCars")),
                train_cars=_operation_value_to_pipe(op.get("TrainCars")),
                passby_path=_operation_value_to_pipe(op.get("PassbyPath")),
            )
        )
    return rows


def _operation_value_to_pipe(value) -> str:
    if isinstance(value, list):
        return "|".join(str(item) for item in value if str(item))
    return str(value or "")


def _render_stage1_trace(
    trace: list[dict],
    summary: dict,
    vehicle_display_labels: dict[str, str],
) -> None:
    reason_counts = Counter(row.get("reason") or "" for row in trace if row.get("accepted"))
    if reason_counts:
        st.markdown("**已执行原因分布**")
        st.dataframe(
            [{"reason": reason, "count": count} for reason, count in reason_counts.most_common()],
            width="stretch",
            hide_index=True,
        )
    rejected_counts: Counter[str] = Counter()
    for row in trace:
        for item in row.get("rejected_before_accept") or []:
            for reason in item.get("violations") or []:
                rejected_counts[str(reason)] += 1
        for item in row.get("rejected") or []:
            for reason in item.get("violations") or []:
                rejected_counts[str(reason)] += 1
    if rejected_counts:
        st.markdown("**候选拒绝原因分布**")
        st.dataframe(
            [{"reason": reason, "count": count} for reason, count in rejected_counts.most_common(30)],
            width="stretch",
            hide_index=True,
        )

    st.markdown("**Trace 明细**")
    trace_rows = []
    for row in trace:
        debt_before = row.get("debt_before") or {}
        debt_after = row.get("debt_after") or {}
        trace_rows.append(
            {
                "hook": row.get("hook"),
                "reason": row.get("reason"),
                "kind": row.get("kind"),
                "source": row.get("source"),
                "target": row.get("target"),
                "move": _replay_format_vehicle_list(row.get("move") or [], vehicle_display_labels),
                "debtBefore": debt_before.get("debt_count"),
                "debtAfter": debt_after.get("debt_count"),
                "blockedGBefore": debt_before.get("blocked_g_count"),
                "blockedGAfter": debt_after.get("blocked_g_count"),
                "path": " | ".join(" -> ".join(path) for path in row.get("paths") or []),
                "warning": row.get("progress_warning", ""),
            }
        )
    st.caption(f"summary blocking reasons: {' | '.join(summary.get('blocking_reasons') or []) or '无'}")
    st.dataframe(trace_rows, width="stretch", hide_index=True)


def _render_stage2_simple_dashboard() -> None:
    st.subheader("第二阶段可视化")
    st.caption("读取 scripts/stage2_simple 的输出，从 Stage1 结束状态开始回放卸轮翻库与大库出库编组。")
    artifact_text = st.text_input(
        "第二阶段输出目录",
        value=str(_stage_simple_output_dir(2)),
        key="stage2-simple-artifact-dir",
    )
    artifact_dir = Path(artifact_text).expanduser()
    _render_stage_simple_io_paths(2, artifact_dir)
    aggregate_path = artifact_dir / "aggregate_summary.json"
    if not aggregate_path.exists():
        st.warning("没有找到 aggregate_summary.json。请先运行 stage2_simple 求解器生成输出。")
        st.code(_stage_simple_run_command(2), language="bash")
        return

    try:
        aggregate = _read_json(aggregate_path)
    except Exception as exc:  # noqa: BLE001
        st.error(f"读取 aggregate_summary.json 失败：{exc}")
        return

    summaries = list(aggregate.get("summaries") or [])
    if not summaries:
        st.warning("aggregate_summary.json 中没有 summaries。")
        return

    case_rows = _stage2_case_rows(summaries, artifact_dir)
    stage1_missing_count = _stage2_reason_count(aggregate, "stage1_artifact_missing")
    if stage1_missing_count:
        st.warning(
            "当前第二阶段目录像是用错误的 --stage1-out 生成的："
            f"{stage1_missing_count} 个案例缺少 Stage1 产物。请重新生成 Stage2 产物。"
        )
        st.code(_stage_simple_run_commands(1, 2), language="bash")
    operation_values = [int(row.get("businessHooks") or 0) for row in case_rows if row.get("status") == "complete"]
    metric_cols = st.columns(8)
    metric_cols[0].metric("案例数", aggregate.get("cases", len(summaries)))
    metric_cols[1].metric("完成", aggregate.get("complete", "-"))
    metric_cols[2].metric("Partial", aggregate.get("partial", "-"))
    metric_cols[3].metric("Unavailable", aggregate.get("unavailable", 0))
    metric_cols[4].metric("Error", aggregate.get("error", 0))
    metric_cols[5].metric("完成均勾", aggregate.get("avg_operations_complete", _average(operation_values)))
    metric_cols[6].metric("完成最大勾", aggregate.get("max_operations_complete", max(operation_values) if operation_values else 0))
    metric_cols[7].metric("Stage2真阻塞", aggregate.get("partial", 0))
    st.caption(
        "口径说明：第二阶段按接口操作行计勾，Get/Put 都算 1 勾；本页不处理称重。"
        "Stage2 的回放起点是 stage2_request，即第一阶段求解后的实际状态。"
        "最终 Put 存4线按 存4南→存4线 口径，且只允许一次并必须是最后一行。"
    )

    filter_cols = st.columns([2, 2, 3])
    status_filter = filter_cols[0].selectbox(
        "状态",
        ["全部", "complete", "partial", "unavailable", "error"],
        key="stage2-status-filter",
    )
    min_hooks = filter_cols[1].number_input("最小业务勾数", min_value=0, value=0, step=1, key="stage2-min-hooks")
    case_query = filter_cols[2].text_input("案例/阻塞原因搜索", value="", key="stage2-case-query")
    filtered_rows = _filter_case_rows(
        case_rows,
        status_filter=status_filter,
        min_hooks=int(min_hooks),
        query=case_query,
    )
    st.markdown("**全量案例**")
    st.caption(f"当前显示 {len(filtered_rows)} / {len(case_rows)} 个案例。")
    st.dataframe(filtered_rows, width="stretch", hide_index=True)

    if not filtered_rows:
        return
    selected_case = st.selectbox(
        "选中案例",
        options=[str(row["caseId"]) for row in filtered_rows],
        format_func=lambda case_id: _stage2_case_label(case_id, filtered_rows),
        key="stage2-selected-case",
    )
    bundle = _stage2_load_case_bundle(artifact_dir, selected_case)
    if not bundle:
        missing = _stage2_missing_case_files(artifact_dir, selected_case)
        detail = f"缺少：{', '.join(missing)}" if missing else "文件读取失败。"
        st.warning(f"案例 {selected_case} 的 stage2_request/response/summary/trace 文件不完整。{detail}")
        return

    summary = bundle["summary"]
    response = bundle["response"]
    trace = bundle["trace"]
    request_payload = bundle["stage2_request"]
    display_response = _stage_response_with_generated(request_payload, response)
    debt = summary.get("stage2_debt") or {}
    operation_rows = _response_operation_rows(response)
    vehicle_display_labels = _replay_vehicle_display_labels(request_payload)

    selected_cols = st.columns(8)
    selected_cols[0].metric("状态", summary.get("status", ""))
    selected_cols[1].metric("业务勾数", _business_hook_count(response))
    selected_cols[2].metric("接口操作", summary.get("operations", 0))
    selected_cols[3].metric("待出库", len(debt.get("pending_stage2_nos") or []))
    selected_cols[4].metric("存4Put次数", summary.get("store4_put_count", 0))
    selected_cols[5].metric("单次最终Put", "是" if summary.get("store4_put_is_final") else "否")
    selected_cols[6].metric("搜索展开", summary.get("expansions", 0))
    selected_cols[7].metric("耗时秒", summary.get("elapsed_seconds", 0))
    if summary.get("blocking_reasons"):
        st.info("阻塞原因：" + " | ".join(summary.get("blocking_reasons") or []))
    if summary.get("waived_replay_differences"):
        st.caption("提示：waived_replay_differences 是共享 replay 仍按渡1入口校验造成的差异；第二阶段最终 Put 存4线按存4南入口口径，不计为硬违规。")

    view = st.radio(
        "查看内容",
        options=["可视化回放", "勾计划", "存4新增段", "终态", "Trace/诊断", "原始 JSON"],
        horizontal=True,
        key="stage2-view",
    )
    if view == "可视化回放":
        _render_replay_replay(request_payload, operation_rows, display_response, vehicle_display_labels, key_prefix="stage2")
    elif view == "勾计划":
        st.markdown("**接口操作序列（Get/Put 为业务勾）**")
        st.dataframe(_replay_operation_table_rows(operation_rows, vehicle_display_labels), width="stretch", hide_index=True)
    elif view == "存4新增段":
        _render_stage2_store4_segment(debt, vehicle_display_labels)
    elif view == "终态":
        _render_replay_end_status(display_response, vehicle_display_labels)
    elif view == "Trace/诊断":
        _render_stage2_trace(trace, summary, vehicle_display_labels)
    else:
        json_cols = st.columns(2)
        with json_cols[0]:
            st.markdown("**summary**")
            st.json(summary)
            st.markdown("**trace**")
            st.json(trace)
        with json_cols[1]:
            st.markdown("**stage2_request**")
            st.json(_replay_response_for_display(request_payload, vehicle_display_labels))
            st.markdown("**response**")
            st.json(_replay_response_for_display(response, vehicle_display_labels))


def _stage2_case_rows(summaries: list[dict], artifact_dir: Path | None = None) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for summary in summaries:
        debt = summary.get("stage2_debt") or {}
        case_id = str(summary.get("case_id") or "")
        response = _try_load_response(artifact_dir, case_id) if artifact_dir is not None else {}
        business_hooks = _business_hook_count(response) if response else int(summary.get("business_hooks") or 0)
        rows.append(
            {
                "caseId": case_id,
                "status": summary.get("status", ""),
                "businessHooks": business_hooks,
                "moveBatches": business_hooks,
                "interfaceOperations": int(summary.get("operations") or 0),
                "pending": len(debt.get("pending_stage2_nos") or []),
                "store4Segment": len(debt.get("new_store4_segment") or []),
                "pattern": debt.get("new_store4_pattern", ""),
                "expansions": int(summary.get("expansions") or 0),
                "elapsedSeconds": summary.get("elapsed_seconds", 0),
                "blockingReasons": " | ".join(summary.get("blocking_reasons") or []),
            }
        )
    return sorted(rows, key=lambda row: (str(row["status"]) != "partial", -int(row["businessHooks"]), str(row["caseId"])))


def _stage2_case_label(case_id: str, rows: list[dict]) -> str:
    row = next((item for item in rows if item.get("caseId") == case_id), {})
    return (
        f"{case_id} | {row.get('status', '')} | "
        f"{row.get('businessHooks', 0)} 业务勾 | {row.get('blockingReasons', '')}"
    )


def _stage2_reason_count(aggregate: dict, reason: str) -> int:
    reasons = aggregate.get("partial_reasons") or {}
    return int(reasons.get(reason) or 0)


def _stage2_load_case_bundle(artifact_dir: Path, case_id: str) -> dict[str, object] | None:
    paths = {
        "summary": artifact_dir / f"{case_id}_summary.json",
        "response": artifact_dir / f"{case_id}_response.json",
        "trace": artifact_dir / f"{case_id}_trace.json",
        "stage2_request": artifact_dir / f"{case_id}_stage2_request.json",
    }
    if not all(path.exists() for path in paths.values()):
        return None
    return {
        key: _read_json(path)
        for key, path in paths.items()
    }


def _stage2_missing_case_files(artifact_dir: Path, case_id: str) -> list[str]:
    paths = [
        artifact_dir / f"{case_id}_summary.json",
        artifact_dir / f"{case_id}_response.json",
        artifact_dir / f"{case_id}_trace.json",
        artifact_dir / f"{case_id}_stage2_request.json",
    ]
    return [path.name for path in paths if not path.exists()]


def _stage_response_with_generated(request_payload: dict, response: dict) -> dict:
    try:
        import replay_validator as replay
    except Exception:  # noqa: BLE001
        return response
    try:
        replayed, _bad = replay.replay(request_payload, response)
    except Exception:  # noqa: BLE001
        return response
    final_rows = [
        {
            "No": replay.car_no(car),
            "Line": car.get("Line"),
            "Position": int(car.get("Position") or 0),
        }
        for car in sorted(replayed, key=lambda item: (item.get("Line") or "", int(item.get("Position") or 0), replay.car_no(item)))
    ]
    output = json.loads(json.dumps(response, ensure_ascii=False))
    data = output.setdefault("Data", {})
    data["GeneratedEndStatus"] = final_rows
    return output


def _render_stage2_store4_segment(debt: dict, vehicle_display_labels: dict[str, str]) -> None:
    segment = list(debt.get("new_store4_segment") or [])
    pattern = str(debt.get("new_store4_pattern") or "")
    st.caption("存4新增段按北→南显示；O=非存4目的车，C=存4目的车。")
    if not segment:
        st.info("没有存4新增段。")
        return
    rows = []
    for index, no in enumerate(segment, start=1):
        rows.append(
            {
                "北侧顺位": index,
                "类型": pattern[index - 1] if index - 1 < len(pattern) else "",
                "车辆": _replay_vehicle_label(no, vehicle_display_labels),
            }
        )
    st.dataframe(rows, width="stretch", hide_index=True)


def _render_stage2_trace(
    trace: list[dict],
    summary: dict,
    vehicle_display_labels: dict[str, str],
) -> None:
    st.caption(f"summary blocking reasons: {' | '.join(summary.get('blocking_reasons') or []) or '无'}")
    rows = []
    for row in trace:
        rows.append(
            {
                "index": row.get("index"),
                "action": row.get("action"),
                "line": row.get("line"),
                "move": _replay_format_vehicle_list(row.get("move") or [], vehicle_display_labels),
                "trainAfter": _replay_format_vehicle_list(row.get("train_after") or [], vehicle_display_labels),
                "path": " -> ".join(row.get("path") or []),
            }
        )
    st.dataframe(rows, width="stretch", hide_index=True)


def _render_stage3_simple_dashboard() -> None:
    st.subheader("第三阶段可视化")
    st.caption("读取 scripts/stage3_simple 的输出，从 Stage2 结束状态开始回放大库落位、库外暂存与终态台位/库外校验。")
    artifact_text = st.text_input(
        "第三阶段输出目录",
        value=str(_stage_simple_output_dir(3)),
        key="stage3-simple-artifact-dir",
    )
    artifact_dir = Path(artifact_text).expanduser()
    if not artifact_dir.is_dir():
        st.warning("第三阶段输出目录不存在。请先运行 stage3_simple 求解器生成输出。")
        st.code(_stage_simple_run_command(3), language="bash")
        return
    _render_stage_simple_io_paths(3, artifact_dir)

    try:
        aggregate = _read_json(artifact_dir / "aggregate_summary.json")
    except Exception as exc:  # noqa: BLE001
        st.error(f"读取第三阶段输出失败：{exc}")
        return

    summaries = list(aggregate.get("summaries") or [])
    if not summaries:
        st.warning("第三阶段输出目录中没有可用 summary。")
        return

    case_rows = _stage3_case_rows(summaries, artifact_dir)
    operation_values = [int(row.get("businessHooks") or 0) for row in case_rows if row.get("status") == "complete"]
    metric_cols = st.columns(9)
    metric_cols[0].metric("案例数", aggregate.get("cases", len(summaries)))
    metric_cols[1].metric("完成", aggregate.get("complete", "-"))
    metric_cols[2].metric("Partial", aggregate.get("partial", "-"))
    metric_cols[3].metric("Unavailable", aggregate.get("unavailable", 0))
    metric_cols[4].metric("Error", aggregate.get("error", 0))
    metric_cols[5].metric("完成均勾", aggregate.get("avg_operations_complete", _average(operation_values)))
    metric_cols[6].metric("完成最大勾", aggregate.get("max_operations_complete", max(operation_values) if operation_values else 0))
    metric_cols[7].metric("Stage3超时", _stage2_reason_count(aggregate, "stage3_global_time_budget_exhausted"))
    metric_cols[8].metric("Replay失败", sum(1 for row in case_rows if not row.get("replayPhysicalOk")))
    st.caption(
        "口径说明：第三阶段业务勾数 = Get/Put 操作数；回放起点是 stage3_request，"
        "终态同时审查库内实际压入台位和库外目标停留。combined replay 仅用于阶段衔接诊断。"
    )

    filter_cols = st.columns([2, 2, 3])
    status_filter = filter_cols[0].selectbox(
        "状态",
        ["全部", "complete", "partial", "unavailable", "error"],
        key="stage3-status-filter",
    )
    min_hooks = filter_cols[1].number_input("最小业务勾数", min_value=0, value=0, step=1, key="stage3-min-hooks")
    case_query = filter_cols[2].text_input("案例/模板/阻塞原因搜索", value="", key="stage3-case-query")
    filtered_rows = _filter_case_rows(
        case_rows,
        status_filter=status_filter,
        min_hooks=int(min_hooks),
        query=case_query,
    )
    st.markdown("**全量案例**")
    st.caption(f"当前显示 {len(filtered_rows)} / {len(case_rows)} 个案例。")
    st.dataframe(filtered_rows, width="stretch", hide_index=True)

    if not filtered_rows:
        return
    selected_case = st.selectbox(
        "选中案例",
        options=[str(row["caseId"]) for row in filtered_rows],
        format_func=lambda case_id: _stage3_case_label(case_id, filtered_rows),
        key="stage3-selected-case",
    )
    bundle = _stage3_load_case_bundle(artifact_dir, selected_case)
    if not bundle:
        st.warning(f"案例 {selected_case} 没有 summary 文件。")
        return

    summary = bundle["summary"]
    response = bundle.get("response") or {"Data": {"Operations": [], "GeneratedEndStatus": []}}
    trace = bundle.get("trace") or []
    request_payload = bundle.get("stage3_request") or _load_truth_payload(selected_case) or {"StartStatus": [], "locoNode": {}}
    combined_response = bundle.get("combined_response") or {}
    operation_rows = _response_operation_rows(response)
    vehicle_display_labels = _replay_vehicle_display_labels(request_payload)

    selected_cols = st.columns(9)
    selected_cols[0].metric("状态", summary.get("status", ""))
    selected_cols[1].metric("模板", summary.get("template", ""))
    selected_cols[2].metric("业务勾数", _business_hook_count(response) or int(summary.get("business_hooks") or 0))
    selected_cols[3].metric("Active车", summary.get("active_count", 0))
    selected_cols[4].metric("终态OK", "是" if summary.get("terminal_depot_ok") else "否")
    selected_cols[5].metric("片段Replay", "是" if summary.get("replay_physical_ok") else "否")
    selected_cols[6].metric("CombinedReplay", "是" if summary.get("combined_replay_physical_ok") else "否")
    selected_cols[7].metric("搜索展开", summary.get("expansions", 0))
    selected_cols[8].metric("耗时秒", summary.get("elapsed_seconds", 0))
    if summary.get("blocking_reasons"):
        st.info("阻塞原因：" + " | ".join(summary.get("blocking_reasons") or []))
    if summary.get("replay_violations"):
        st.warning(f"片段 replay 违规 {len(summary.get('replay_violations') or [])} 条。")
    if summary.get("combined_replay_violations"):
        st.caption(
            "combined replay 违规用于审查 Stage2→Stage3 衔接；"
            "当前 Stage3 片段成功与否以上方片段 replay/终态校验为准。"
        )

    view = st.radio(
        "查看内容",
        options=["可视化回放", "勾计划", "终态", "Trace/诊断", "模板/校验", "原始 JSON"],
        horizontal=True,
        key="stage3-view",
    )
    if view == "可视化回放":
        if not operation_rows:
            st.info("当前案例没有可回放的第三阶段操作。")
        else:
            _render_replay_replay(request_payload, operation_rows, response, vehicle_display_labels, key_prefix="stage3")
    elif view == "勾计划":
        if operation_rows:
            st.markdown("**接口操作序列（Get/Put 为业务勾）**")
            st.dataframe(_replay_operation_table_rows(operation_rows, vehicle_display_labels), width="stretch", hide_index=True)
        else:
            st.info("当前没有生成操作。")
    elif view == "终态":
        _render_replay_end_status(response, vehicle_display_labels)
    elif view == "Trace/诊断":
        _render_stage3_trace(trace, summary, vehicle_display_labels)
    elif view == "模板/校验":
        _render_stage3_template_and_validation(summary)
    else:
        json_cols = st.columns(2)
        with json_cols[0]:
            st.markdown("**summary**")
            st.json(summary)
            st.markdown("**trace**")
            st.json(trace)
        with json_cols[1]:
            st.markdown("**stage3_request**")
            st.json(_replay_response_for_display(request_payload, vehicle_display_labels))
            st.markdown("**response**")
            st.json(_replay_response_for_display(response, vehicle_display_labels))
            if combined_response:
                st.markdown("**combined_response**")
                st.json(_replay_response_for_display(combined_response, vehicle_display_labels))


def _stage3_case_rows(summaries: list[dict], artifact_dir: Path | None = None) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for summary in summaries:
        case_id = str(summary.get("case_id") or "")
        response = _try_load_response(artifact_dir, case_id) if artifact_dir is not None else {}
        business_hooks = _business_hook_count(response) if response else int(summary.get("business_hooks") or summary.get("operations") or 0)
        blocking_reasons = " | ".join(summary.get("blocking_reasons") or [])
        rows.append(
            {
                "caseId": case_id,
                "status": summary.get("status", ""),
                "template": summary.get("template", ""),
                "businessHooks": business_hooks,
                "moveBatches": business_hooks,
                "interfaceOperations": int(summary.get("operations") or 0),
                "activeCount": int(summary.get("active_count") or 0),
                "terminalDepotOk": bool(summary.get("terminal_depot_ok")),
                "replayPhysicalOk": bool(summary.get("replay_physical_ok")),
                "combinedReplayOk": bool(summary.get("combined_replay_physical_ok")),
                "expansions": int(summary.get("expansions") or 0),
                "elapsedSeconds": summary.get("elapsed_seconds", 0),
                "blockingReasons": blocking_reasons,
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("status")) != "partial",
            not bool(row.get("terminalDepotOk")),
            -int(row.get("businessHooks") or 0),
            str(row.get("caseId")),
        ),
    )


def _stage3_case_label(case_id: str, rows: list[dict]) -> str:
    row = next((item for item in rows if item.get("caseId") == case_id), {})
    return (
        f"{case_id} | {row.get('status', '')} | 模板 {row.get('template', '')} | "
        f"{row.get('businessHooks', 0)} 业务勾 | {row.get('blockingReasons', '')}"
    )


def _stage3_load_case_bundle(artifact_dir: Path, case_id: str) -> dict[str, object] | None:
    summary_path = artifact_dir / f"{case_id}_summary.json"
    if not summary_path.exists():
        return None
    paths = {
        "summary": summary_path,
        "response": artifact_dir / f"{case_id}_response.json",
        "trace": artifact_dir / f"{case_id}_trace.json",
        "stage3_request": artifact_dir / f"{case_id}_stage3_request.json",
        "combined_response": artifact_dir / f"{case_id}_combined_response.json",
    }
    bundle: dict[str, object] = {"summary": _read_json(summary_path)}
    for key, path in paths.items():
        if key == "summary" or not path.exists():
            continue
        try:
            bundle[key] = _read_json(path)
        except Exception:  # noqa: BLE001
            continue
    return bundle


def _render_stage3_trace(
    trace: list[dict],
    summary: dict,
    vehicle_display_labels: dict[str, str],
) -> None:
    st.caption(f"summary blocking reasons: {' | '.join(summary.get('blocking_reasons') or []) or '无'}")
    if not trace:
        st.info("当前没有 stage3 trace。")
        return
    action_counts = Counter(str(row.get("action") or "") for row in trace)
    st.markdown("**动作分布**")
    st.dataframe(
        [{"action": action, "count": count} for action, count in action_counts.most_common()],
        width="stretch",
        hide_index=True,
    )
    rows = []
    for row in trace:
        rows.append(
            {
                "index": row.get("index"),
                "action": row.get("action"),
                "line": row.get("line"),
                "move": _replay_format_vehicle_list(row.get("move") or [], vehicle_display_labels),
                "trainAfter": _replay_format_vehicle_list(row.get("train_after") or [], vehicle_display_labels),
                "path": " -> ".join(row.get("path") or []),
                "note": row.get("note", ""),
            }
        )
    st.markdown("**Trace 明细**")
    st.dataframe(rows, width="stretch", hide_index=True)


def _render_stage3_template_and_validation(summary: dict) -> None:
    template_rows = list(summary.get("template_summaries") or [])
    if template_rows:
        st.markdown("**模板求解摘要**")
        st.dataframe(template_rows, width="stretch", hide_index=True)
    else:
        st.info("summary 中没有 template_summaries。")

    validation_cols = st.columns(4)
    validation_cols[0].metric("终态台位/库外", "OK" if summary.get("terminal_depot_ok") else "FAIL")
    validation_cols[1].metric("Stage3片段Replay", "OK" if summary.get("replay_physical_ok") else "FAIL")
    validation_cols[2].metric("CombinedReplay", "OK" if summary.get("combined_replay_physical_ok") else "FAIL")
    validation_cols[3].metric("Active车", summary.get("active_count", 0))

    replay_violations = list(summary.get("replay_violations") or [])
    combined_violations = list(summary.get("combined_replay_violations") or [])
    if replay_violations:
        st.markdown("**Stage3片段 replay violations**")
        st.dataframe(replay_violations, width="stretch", hide_index=True)
    if combined_violations:
        st.markdown("**Combined replay violations**")
        st.dataframe(combined_violations, width="stretch", hide_index=True)


def _render_stage4_simple_dashboard() -> None:
    st.subheader("第四阶段可视化")
    st.caption("读取 scripts/stage4_simple 的输出，从 Stage3 结束状态开始回放剩余调车收口、终态未满足和证明/阻塞诊断。")
    artifact_text = st.text_input(
        "第四阶段输出目录",
        value=str(_stage_simple_output_dir(4)),
        key="stage4-simple-artifact-dir",
    )
    artifact_dir = Path(artifact_text).expanduser()
    if not artifact_dir.is_dir():
        st.warning("第四阶段输出目录不存在。请先运行 stage4_simple 求解器生成输出。")
        st.code(_stage_simple_run_command(4), language="bash")
        return
    _render_stage_simple_io_paths(4, artifact_dir)

    try:
        aggregate = _read_json(artifact_dir / "aggregate_summary.json")
    except Exception as exc:  # noqa: BLE001
        st.error(f"读取第四阶段输出失败：{exc}")
        return

    summaries = list(aggregate.get("summaries") or [])
    if not summaries:
        st.warning("第四阶段输出目录中没有可用 summary。")
        return

    case_rows = _stage4_case_rows(summaries, artifact_dir)
    actionable_rows = [row for row in case_rows if row.get("actionableComplete")]
    operation_values = [
        int(row.get("businessHooks") or 0)
        for row in actionable_rows
    ]
    metric_cols = st.columns(8)
    metric_cols[0].metric("案例数", aggregate.get("cases", len(summaries)))
    metric_cols[1].metric("完成", aggregate.get("complete", "-"))
    metric_cols[2].metric("可行动闭合", aggregate.get("actionable_complete", len(actionable_rows)))
    metric_cols[3].metric("容量留置", aggregate.get("capacity_limited", 0))
    metric_cols[4].metric("Active残余", aggregate.get("active_residual", 0))
    metric_cols[5].metric("闭合均勾", aggregate.get("avg_business_hooks_actionable", _average(operation_values)))
    metric_cols[6].metric("Stage3不可用", aggregate.get("unavailable", 0))
    metric_cols[7].metric(
        "Replay失败",
        sum(
            1
            for row in case_rows
            if row.get("replayPhysicalOk") is False or row.get("combinedReplayOk") is False
        ),
    )
    st.caption(
        "口径说明：第四阶段业务勾数 = Get/Put 操作数；回放起点是 stage4_request。"
        "status 只使用 complete/partial/unavailable/error；可行动闭合与容量留置通过 stage4_debt 单独统计。"
    )

    filter_cols = st.columns([2, 2, 3])
    status_filter = filter_cols[0].selectbox(
        "状态",
        ["全部", "complete", "partial", "unavailable", "error"],
        key="stage4-status-filter",
    )
    min_hooks = filter_cols[1].number_input("最小业务勾数", min_value=0, value=0, step=1, key="stage4-min-hooks")
    case_query = filter_cols[2].text_input("案例/最优性/阻塞原因搜索", value="", key="stage4-case-query")
    filtered_rows = _filter_case_rows(
        case_rows,
        status_filter=status_filter,
        min_hooks=int(min_hooks),
        query=case_query,
    )
    st.markdown("**全量案例**")
    st.caption(f"当前显示 {len(filtered_rows)} / {len(case_rows)} 个案例。")
    st.dataframe(filtered_rows, width="stretch", hide_index=True)

    if not filtered_rows:
        return
    selected_case = st.selectbox(
        "选中案例",
        options=[str(row["caseId"]) for row in filtered_rows],
        format_func=lambda case_id: _stage4_case_label(case_id, filtered_rows),
        key="stage4-selected-case",
    )
    bundle = _stage4_load_case_bundle(artifact_dir, selected_case)
    if not bundle:
        st.warning(f"案例 {selected_case} 没有 summary 文件。")
        return

    summary = bundle["summary"]
    response = bundle.get("response") or {"Data": {"Operations": []}}
    trace = bundle.get("trace") or []
    request_payload = bundle.get("stage4_request") or _load_truth_payload(selected_case) or {"StartStatus": [], "locoNode": {}}
    combined_response = bundle.get("combined_response") or {}
    display_response = _stage_response_with_generated(request_payload, response)
    operation_rows = _response_operation_rows(response)
    vehicle_display_labels = _replay_vehicle_display_labels(request_payload)

    selected_cols = st.columns(10)
    selected_cols[0].metric("状态", summary.get("status", ""))
    selected_cols[1].metric("最优性", summary.get("optimality", ""))
    selected_cols[2].metric("业务勾数", _business_hook_count(response) or int(summary.get("business_hooks") or 0))
    selected_cols[3].metric("Active车", summary.get("active_count", 0))
    selected_cols[4].metric("越界债务", summary.get("out_of_scope_count", 0))
    selected_cols[5].metric("终态未满足", summary.get("final_unsatisfied_count", 0))
    selected_cols[6].metric("片段Replay", _stage_yes_no(summary.get("replay_physical_ok")))
    selected_cols[7].metric("CombinedReplay", _stage_yes_no(summary.get("combined_replay_physical_ok")))
    selected_cols[8].metric("搜索展开", summary.get("expansions", 0))
    selected_cols[9].metric("耗时秒", summary.get("elapsed_seconds", 0))
    if summary.get("blocking_reasons"):
        st.info("阻塞原因：" + " | ".join(summary.get("blocking_reasons") or []))
    if summary.get("replay_violations"):
        st.warning(f"片段 replay 违规 {len(summary.get('replay_violations') or [])} 条。")
    if summary.get("combined_replay_violations"):
        st.caption("combined replay 违规用于审查 Stage1→Stage4 全链路衔接。")

    view = st.radio(
        "查看内容",
        options=["可视化回放", "勾计划", "终态", "Trace/诊断", "证明/校验", "原始 JSON"],
        horizontal=True,
        key="stage4-view",
    )
    if view == "可视化回放":
        _render_replay_replay(request_payload, operation_rows, display_response, vehicle_display_labels, key_prefix="stage4")
    elif view == "勾计划":
        if operation_rows:
            st.markdown("**接口操作序列（Get/Put 为业务勾）**")
            st.dataframe(_replay_operation_table_rows(operation_rows, vehicle_display_labels), width="stretch", hide_index=True)
        else:
            st.info("当前没有生成操作。")
    elif view == "终态":
        _render_replay_end_status(display_response, vehicle_display_labels)
    elif view == "Trace/诊断":
        _render_stage4_trace(trace, summary, vehicle_display_labels)
    elif view == "证明/校验":
        _render_stage4_proof_and_validation(summary)
    else:
        json_cols = st.columns(2)
        with json_cols[0]:
            st.markdown("**summary**")
            st.json(summary)
            st.markdown("**trace**")
            st.json(trace)
        with json_cols[1]:
            st.markdown("**stage4_request**")
            st.json(_replay_response_for_display(request_payload, vehicle_display_labels))
            st.markdown("**response**")
            st.json(_replay_response_for_display(response, vehicle_display_labels))
            if combined_response:
                st.markdown("**combined_response**")
                st.json(_replay_response_for_display(combined_response, vehicle_display_labels))


def _stage_yes_no(value) -> str:
    if value is None:
        return "未知"
    return "是" if value else "否"


def _stage4_case_rows(summaries: list[dict], artifact_dir: Path | None = None) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for summary in summaries:
        case_id = str(summary.get("case_id") or "")
        response = _try_load_response(artifact_dir, case_id) if artifact_dir is not None else {}
        business_hooks = _business_hook_count(response) if response else int(summary.get("business_hooks") or summary.get("operations") or 0)
        blocking_reasons = " | ".join(summary.get("blocking_reasons") or [])
        rows.append(
            {
                "caseId": case_id,
                "status": summary.get("status", ""),
                "optimality": summary.get("optimality", ""),
                "actionableComplete": bool(
                    summary.get("status") == "complete"
                    or (summary.get("stage4_debt") or {}).get("actionable_complete")
                ),
                "businessHooks": business_hooks,
                "moveBatches": business_hooks,
                "interfaceOperations": int(summary.get("operations") or 0),
                "activeCount": int(summary.get("active_count") or 0),
                "outOfScope": int(summary.get("out_of_scope_count") or 0),
                "finalUnsatisfied": int(summary.get("final_unsatisfied_count") or 0),
                "replayPhysicalOk": summary.get("replay_physical_ok"),
                "combinedReplayOk": summary.get("combined_replay_physical_ok"),
                "expansions": int(summary.get("expansions") or 0),
                "elapsedSeconds": summary.get("elapsed_seconds", 0),
                "blockingReasons": blocking_reasons,
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("status")) not in {"partial", "error"},
            row.get("replayPhysicalOk") is False,
            -int(row.get("businessHooks") or 0),
            str(row.get("caseId")),
        ),
    )


def _stage4_case_label(case_id: str, rows: list[dict]) -> str:
    row = next((item for item in rows if item.get("caseId") == case_id), {})
    return (
        f"{case_id} | {row.get('status', '')} | {row.get('optimality', '')} | "
        f"{row.get('businessHooks', 0)} 业务勾 | {row.get('blockingReasons', '')}"
    )


def _stage4_load_case_bundle(artifact_dir: Path, case_id: str) -> dict[str, object] | None:
    summary_path = artifact_dir / f"{case_id}_summary.json"
    if not summary_path.exists():
        return None
    paths = {
        "summary": summary_path,
        "response": artifact_dir / f"{case_id}_response.json",
        "trace": artifact_dir / f"{case_id}_trace.json",
        "stage4_request": artifact_dir / f"{case_id}_stage4_request.json",
        "combined_response": artifact_dir / f"{case_id}_combined_response.json",
    }
    bundle: dict[str, object] = {"summary": _read_json(summary_path)}
    for key, path in paths.items():
        if key == "summary" or not path.exists():
            continue
        try:
            bundle[key] = _read_json(path)
        except Exception:  # noqa: BLE001
            continue
    return bundle


def _render_stage4_trace(
    trace: list[dict],
    summary: dict,
    vehicle_display_labels: dict[str, str],
) -> None:
    st.caption(f"summary blocking reasons: {' | '.join(summary.get('blocking_reasons') or []) or '无'}")
    if not trace:
        st.info("当前没有 stage4 trace。")
        return
    action_counts = Counter(str(row.get("action") or "") for row in trace)
    st.markdown("**动作分布**")
    st.dataframe(
        [{"action": action, "count": count} for action, count in action_counts.most_common()],
        width="stretch",
        hide_index=True,
    )
    rows = []
    for row in trace:
        rows.append(
            {
                "index": row.get("index"),
                "action": row.get("action"),
                "line": row.get("line"),
                "move": _replay_format_vehicle_list(row.get("move") or [], vehicle_display_labels),
                "trainAfter": _replay_format_vehicle_list(row.get("train_after") or [], vehicle_display_labels),
                "path": " -> ".join(row.get("path") or []),
                "note": row.get("note", ""),
            }
        )
    st.markdown("**Trace 明细**")
    st.dataframe(rows, width="stretch", hide_index=True)


def _render_stage4_proof_and_validation(summary: dict) -> None:
    validation_cols = st.columns(5)
    validation_cols[0].metric("最优性", summary.get("optimality", ""))
    validation_cols[1].metric("Stage4片段Replay", "OK" if summary.get("replay_physical_ok") else "FAIL/未知")
    validation_cols[2].metric("CombinedReplay", _stage_yes_no(summary.get("combined_replay_physical_ok")))
    validation_cols[3].metric("终态未满足", summary.get("final_unsatisfied_count", 0))
    validation_cols[4].metric("越界债务", summary.get("out_of_scope_count", 0))

    proof = summary.get("proof") or {}
    if proof:
        st.markdown("**搜索证明**")
        st.json(proof)

    restrictions = list(summary.get("move_model_restrictions") or [])
    if restrictions:
        st.markdown("**Move Model 约束**")
        st.dataframe([{"restriction": item} for item in restrictions], width="stretch", hide_index=True)

    final_unsatisfied = list(summary.get("final_unsatisfied_nos") or [])
    if final_unsatisfied:
        st.markdown("**终态未满足车辆**")
        st.dataframe([{"vehicleNo": item} for item in final_unsatisfied], width="stretch", hide_index=True)

    replay_violations = list(summary.get("replay_violations") or [])
    combined_violations = list(summary.get("combined_replay_violations") or [])
    if replay_violations:
        st.markdown("**Stage4片段 replay violations**")
        st.dataframe(replay_violations, width="stretch", hide_index=True)
    if combined_violations:
        st.markdown("**Combined replay violations**")
        st.dataframe(combined_violations, width="stretch", hide_index=True)



if __name__ == "__main__":
    main()
