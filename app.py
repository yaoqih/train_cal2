from __future__ import annotations

import base64
from collections import defaultdict
from dataclasses import asdict
from functools import lru_cache
import hashlib
import importlib
from io import BytesIO
import json
from pathlib import Path
from html import escape
import re
import sys

import streamlit as st
from PIL import Image

try:
    from fzed_shunting.demo.layout import (
        build_route_polyline,
        load_topology_layout,
        LayoutPoint,
        point_at_progress,
        route_to_svg_path,
    )
    from fzed_shunting.demo.schematic import build_schematic_route, load_schematic_layout
    from fzed_shunting.demo.view_model import (
        build_demo_workflow_view_model,
        select_demo_payload,
    )
    from fzed_shunting.domain.master_data import load_master_data
    from fzed_shunting.solver.profile import (
        VALIDATION_DEFAULT_BEAM_WIDTH,
        VALIDATION_DEFAULT_SOLVER,
        VALIDATION_DEFAULT_TIMEOUT_SECONDS,
        validation_time_budget_ms,
    )
    from fzed_shunting.tools.segmented_routes_svg import load_segmented_physical_routes
    from fzed_shunting.workflow.l7_closed_topology_mode import (
        OPERATION_MODE_L7_CLOSED_TOPOLOGY,
        is_l7_closed_topology_mode,
    )

    _FZED_IMPORT_ERROR = None
except ModuleNotFoundError as exc:
    build_route_polyline = None
    load_topology_layout = None
    LayoutPoint = None
    point_at_progress = None
    route_to_svg_path = None
    build_schematic_route = None
    load_schematic_layout = None
    build_demo_workflow_view_model = None
    select_demo_payload = None
    load_master_data = None
    load_segmented_physical_routes = None
    is_l7_closed_topology_mode = None
    VALIDATION_DEFAULT_BEAM_WIDTH = 8
    VALIDATION_DEFAULT_SOLVER = "beam"
    VALIDATION_DEFAULT_TIMEOUT_SECONDS = 60.0
    OPERATION_MODE_L7_CLOSED_TOPOLOGY = "L7_CLOSED_TOPOLOGY"
    _FZED_IMPORT_ERROR = exc

    def validation_time_budget_ms(timeout_seconds: float) -> float:
        return max(0.0, float(timeout_seconds) * 1000.0 - 5000.0)


MASTER_DIR = Path(__file__).resolve().parent / "data" / "master"
ROOT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
TRUTH2_DIR = ROOT_DIR / "data" / "truth2"
P10_SCHEMATIC_LAYOUT_PATH = ROOT_DIR / "data" / "map" / "schematic_layout.json"
P10_RUNTIME_SCRIPT_PATH = SCRIPTS_DIR / "generate_physical_runtime_trace.py"
P10_PHASE_GATE_SCRIPT_PATH = SCRIPTS_DIR / "validate_phase_gates.py"
APP_P10_OUTPUT_DIR = ROOT_DIR / "artifacts" / "app_p10_runtime"
DEFAULT_EVAL_ARTIFACT = (
    Path(__file__).resolve().parent
    / "artifacts"
    / "l7_phase1234_truth_phase3_tail_run_preflight_20260614.json"
)
_TOPOLOGY_LAYOUT = None
_SCHEMATIC_LAYOUT = None
_MASTER_DATA = None
_P10_RUNTIME_CACHE = None
P10_BUSINESS_HOOK_ACTIONS = {"Get", "Put"}


def _get_master_data():
    if load_master_data is None:
        raise RuntimeError(f"旧案例回放依赖 fzed_shunting 不可用：{_FZED_IMPORT_ERROR}")
    global _MASTER_DATA
    if _MASTER_DATA is None:
        _MASTER_DATA = load_master_data(MASTER_DIR)
    return _MASTER_DATA


def _p10_file_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return 0


def _p10_runtime_fingerprint() -> tuple[tuple[str, int], ...]:
    return (
        ("generate_physical_runtime_trace.py", _p10_file_mtime_ns(P10_RUNTIME_SCRIPT_PATH)),
        ("validate_phase_gates.py", _p10_file_mtime_ns(P10_PHASE_GATE_SCRIPT_PATH)),
    )


def _p10_runtime_version_text() -> str:
    digest = hashlib.sha1(repr(_p10_runtime_fingerprint()).encode("utf-8")).hexdigest()[:8]
    return f"P10 runtime {digest}"


def _p10_runtime_module():
    global _P10_RUNTIME_CACHE
    fingerprint = _p10_runtime_fingerprint()
    if _P10_RUNTIME_CACHE is not None and _P10_RUNTIME_CACHE[0] == fingerprint:
        return _P10_RUNTIME_CACHE[1]

    scripts_path = str(SCRIPTS_DIR)
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)
    if "validate_phase_gates" in sys.modules:
        importlib.reload(sys.modules["validate_phase_gates"])
    if "generate_physical_runtime_trace" in sys.modules:
        p10_runtime = importlib.reload(sys.modules["generate_physical_runtime_trace"])
    else:
        import generate_physical_runtime_trace as p10_runtime  # noqa: PLC0415

    _P10_RUNTIME_CACHE = (fingerprint, p10_runtime)
    return p10_runtime


def _payload_cache_key(payload) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def _validation_time_budget_ms(timeout_seconds: float) -> float:
    return validation_time_budget_ms(timeout_seconds)


def _as_l7_workflow_payload(payload: dict) -> dict:
    if _is_workflow_payload(payload):
        return dict(payload)
    workflow_payload = dict(payload)
    workflow_payload["operationMode"] = OPERATION_MODE_L7_CLOSED_TOPOLOGY
    return workflow_payload


@st.cache_data(show_spinner=False, max_entries=16)
def _cached_workflow_view_model(
    payload_key: str,
    solver: str,
    heuristic_weight: float,
    beam_width: int | None,
    time_budget_ms: float | None,
):
    if build_demo_workflow_view_model is None:
        raise RuntimeError(f"旧案例回放依赖 fzed_shunting 不可用：{_FZED_IMPORT_ERROR}")
    payload = json.loads(payload_key)
    return build_demo_workflow_view_model(
        _get_master_data(),
        payload,
        solver=solver,
        heuristic_weight=heuristic_weight,
        beam_width=beam_width,
        time_budget_ms=time_budget_ms,
    )


def main():
    st.set_page_config(page_title="福州东调车 Demo", layout="wide")
    st.title("福州东调车 Demo")
    st.caption("输入取送车计划，运行 P10 物理求解演示，并保留原 block 级案例回放与评估统计。")

    p10_tab, demo_tab, eval_tab = st.tabs(["P10 求解演示", "案例回放", "评估统计"])
    with p10_tab:
        _render_p10_runtime_page()
    with eval_tab:
        _render_evaluation_dashboard()
    with demo_tab:
        _render_single_scenario_page()


@st.cache_data(show_spinner=False)
def _p10_truth2_options() -> list[str]:
    if not TRUTH2_DIR.exists():
        return []
    paths = [
        path
        for path in TRUTH2_DIR.glob("*.json")
        if path.name != "conversion_summary.json"
    ]
    paths.sort(key=lambda path: (_p10_try_case_id_from_text(path.name) or path.name, path.name))
    return [str(path) for path in paths]


@st.cache_resource(show_spinner=False)
def _p10_track_graph(runtime_fingerprint: tuple[tuple[str, int], ...]):
    return _p10_runtime_module().TrackGraph()


def _render_p10_runtime_page() -> None:
    st.subheader("P10 求解演示")
    st.caption("读取接口形态 JSON，调用 P10 runtime 输出 Operations 和 GeneratedEndStatus。")

    source_kind = st.radio(
        "计划来源",
        options=["内置 truth2", "本机路径", "上传 JSON", "粘贴 JSON"],
        horizontal=True,
        key="p10-source-kind",
    )

    input_path: Path | None = None
    payload: dict | None = None
    source_name = ""
    uploaded_file = None
    pasted_text = ""

    if source_kind == "内置 truth2":
        option_paths = _p10_truth2_options()
        if not option_paths:
            st.warning("data/truth2 下没有可用 JSON。")
            return
        selected_path_text = st.selectbox(
            "truth2 案例",
            options=option_paths,
            index=_p10_default_truth2_index(option_paths),
            format_func=_p10_truth2_label,
            key="p10-truth2-case",
        )
        input_path = Path(selected_path_text)
        source_name = input_path.name
        st.code(str(input_path), language="text")
    elif source_kind == "本机路径":
        path_text = st.text_input("计划 JSON 路径", value="", key="p10-local-path")
        if path_text.strip():
            input_path = Path(path_text).expanduser()
            source_name = input_path.name
    elif source_kind == "上传 JSON":
        uploaded_file = st.file_uploader("上传计划 JSON", type=["json"], key="p10-upload")
        if uploaded_file is not None:
            source_name = uploaded_file.name
    else:
        pasted_text = st.text_area("粘贴计划 JSON", height=260, key="p10-pasted-json")
        source_name = "pasted_plan_9999Z.json"

    st.caption(
        f"{_p10_runtime_version_text()}；业务对人工比较应看挂/摘勾数，"
        "runtime 内部的批次号只是一次取放搬运的分组。"
    )
    max_hooks = st.number_input(
        "最大内部移动批次",
        min_value=1,
        max_value=1000,
        value=280,
        step=10,
        key="p10-max-hooks",
    )
    run_requested = st.button("运行 P10 求解", type="primary", key="p10-run")
    if not run_requested:
        last_result = st.session_state.get("p10_last_result")
        if last_result:
            stale_reason = _p10_stale_result_reason(last_result, source_name)
            st.divider()
            if stale_reason:
                st.warning(stale_reason)
                if st.button("清空过期结果", key="p10-clear-stale-result"):
                    st.session_state.pop("p10_last_result", None)
                    st.rerun()
                return
            st.caption("显示上一次 P10 求解结果；修改输入后需要重新点击“运行 P10 求解”才会更新。")
            if st.button("清空上一次结果", key="p10-clear-last-result"):
                st.session_state.pop("p10_last_result", None)
                st.rerun()
            _render_p10_result(**last_result)
        return

    try:
        if source_kind == "上传 JSON":
            if uploaded_file is None:
                st.error("请先上传 JSON 文件。")
                return
            payload = json.loads(uploaded_file.getvalue().decode("utf-8-sig"))
        elif source_kind == "粘贴 JSON":
            if not pasted_text.strip():
                st.error("请先粘贴 JSON。")
                return
            payload = json.loads(pasted_text)
        elif input_path is None:
            st.error("请先选择或填写计划 JSON。")
            return

        runtime_input_path, runtime_payload = _p10_resolve_runtime_input_path(
            input_path=input_path,
            payload=payload,
            source_name=source_name,
        )
    except Exception as exc:  # noqa: BLE001
        st.error(f"计划 JSON 读取失败：{exc}")
        return

    try:
        with st.spinner("P10 求解中..."):
            p10 = _p10_runtime_module()
            summary, candidate_rows, operation_rows, rejection_reasons = p10.run_case(
                truth_path=runtime_input_path,
                output_dir=APP_P10_OUTPUT_DIR,
                graph=_p10_track_graph(_p10_runtime_fingerprint()),
                max_hooks=int(max_hooks),
            )
            response = _p10_load_response_or_build(summary, operation_rows, runtime_payload)
    except Exception as exc:  # noqa: BLE001
        st.exception(exc)
        return

    result = {
        "payload": runtime_payload,
        "runtime_input_path": runtime_input_path,
        "summary": summary,
        "candidate_rows": candidate_rows,
        "operation_rows": operation_rows,
        "rejection_reasons": rejection_reasons,
        "response": response,
        "runtime_fingerprint": _p10_runtime_fingerprint(),
        "source_name": source_name,
    }
    st.session_state["p10_last_result"] = result
    st.session_state["p10_replay_frame_index"] = 0
    _render_p10_result(**result)


def _p10_truth2_label(path_text: str) -> str:
    path = Path(path_text)
    case_id = _p10_try_case_id_from_text(path.name) or "未知案例"
    return f"{case_id} | {path.name}"


def _p10_default_truth2_index(option_paths: list[str]) -> int:
    for index, path_text in enumerate(option_paths):
        if _p10_try_case_id_from_text(Path(path_text).name) == "0104W":
            return index
    return 0


def _p10_stale_result_reason(last_result: dict, source_name: str) -> str:
    if last_result.get("runtime_fingerprint") != _p10_runtime_fingerprint():
        return "上一次 P10 结果由旧 runtime 生成，已不再展示；请重新点击“运行 P10 求解”。"
    last_source = str(last_result.get("source_name") or "")
    if last_source and source_name and last_source != source_name:
        return "上一次 P10 结果属于另一个计划，已不再展示；请重新点击“运行 P10 求解”。"
    return ""


def _p10_try_case_id_from_text(text: str) -> str | None:
    match = re.search(r"(\d{4}[ZWzw])", text or "")
    return match.group(1).upper() if match else None


def _p10_resolve_runtime_input_path(
    *,
    input_path: Path | None,
    payload: dict | None,
    source_name: str,
) -> tuple[Path, dict]:
    if input_path is not None:
        if not input_path.exists():
            raise FileNotFoundError(str(input_path))
        loaded_payload = payload if payload is not None else _p10_read_json(input_path)
        if _p10_try_case_id_from_text(input_path.name):
            return input_path, loaded_payload
        return _p10_write_app_plan_input(loaded_payload, source_name or input_path.name), loaded_payload

    if payload is None:
        raise ValueError("payload is empty")
    return _p10_write_app_plan_input(payload, source_name), payload


def _p10_read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("JSON 根节点必须是对象")
    return value


def _p10_write_app_plan_input(payload: dict, source_name: str) -> Path:
    if not isinstance(payload, dict):
        raise ValueError("JSON 根节点必须是对象")
    case_id = _p10_try_case_id_from_text(source_name) or "9999Z"
    digest = hashlib.sha1(_payload_cache_key(payload).encode("utf-8")).hexdigest()[:10]
    input_dir = APP_P10_OUTPUT_DIR / "inputs"
    input_dir.mkdir(parents=True, exist_ok=True)
    path = input_dir / f"app_plan_{case_id}_{digest}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _p10_load_response_or_build(summary, operation_rows, payload: dict) -> dict:
    response_path = Path(summary.response_path) if summary.response_path else None
    if response_path is not None and response_path.exists():
        return json.loads(response_path.read_text(encoding="utf-8"))
    p10 = _p10_runtime_module()
    success = summary.status == "completed"
    return {
        "Success": success,
        "Message": "" if success else summary.blocked_reason,
        "StatusCode": 200 if success else 409,
        "Data": {
            "Operations": [p10.response_operation(row) for row in operation_rows],
            "GeneratedEndStatus": _p10_generated_status_from_payload(payload),
        },
    }


def _render_p10_result(
    *,
    payload: dict,
    runtime_input_path: Path,
    summary,
    candidate_rows,
    operation_rows,
    rejection_reasons,
    response: dict,
    runtime_fingerprint=None,
    source_name: str = "",
) -> None:
    summary_dict = asdict(summary)
    status = summary_dict["status"]
    status_text = _p10_status_text(status)
    business_hook_count = _p10_business_hook_count(operation_rows)
    action_counts = _p10_action_counts(operation_rows)
    if status == "completed":
        st.success("P10 已完成求解并生成接口响应。")
    elif status == "invalid_input":
        st.error("输入不符合 P10 runtime 当前接口要求。")
    else:
        st.warning("P10 未完成求解，页面展示当前阻塞诊断。")
    if summary_dict["blocked_reason"]:
        st.info(f"阻塞原因：{_p10_format_blocked_reason(summary_dict['blocked_reason'])}")

    metric_cols = st.columns(8)
    metric_cols[0].metric("状态", status_text)
    metric_cols[1].metric("车辆数", summary_dict["vehicle_count"])
    metric_cols[2].metric("初始未满足", summary_dict["initial_unsatisfied_vehicle_count"])
    metric_cols[3].metric("最终未满足", summary_dict["final_unsatisfied_vehicle_count"])
    metric_cols[4].metric("业务挂摘勾数", business_hook_count)
    metric_cols[5].metric("内部移动批次", summary_dict["generated_hook_count"])
    metric_cols[6].metric("接口操作数", summary_dict["generated_operation_count"])
    metric_cols[7].metric("硬违规接收", summary_dict["hard_physical_violation_accepted_count"])
    remote_cols = st.columns(4)
    remote_cols[0].metric("求解策略", summary_dict.get("solve_strategy", "未知"))
    remote_cols[1].metric("远端跨区次数", summary_dict.get("remote_interaction_cross_count", 0))
    remote_cols[2].metric("远端相关批次", summary_dict.get("remote_interaction_batch_count", 0))
    remote_cols[3].metric("远端会话数", summary_dict.get("remote_interaction_session_count", 0))
    st.caption(
        "口径说明：业务挂摘勾数 = Get/Put 次数，用于和人工勾数比较；"
        "内部移动批次 = runtime 一次取放搬运分组；接口操作数 = Operations 条数，包含称重等非挂摘操作。"
    )

    guard_rows = [
        {"check": "business_get_put_hook_count", "value": business_hook_count},
        {"check": "get_count", "value": action_counts.get("Get", 0)},
        {"check": "put_count", "value": action_counts.get("Put", 0)},
        {"check": "weigh_operation_count", "value": action_counts.get("Weigh", 0)},
        {"check": "solve_strategy", "value": summary_dict.get("solve_strategy", "未知")},
        {"check": "remote_interaction_cross_count", "value": summary_dict.get("remote_interaction_cross_count", 0)},
        {"check": "remote_interaction_batch_count", "value": summary_dict.get("remote_interaction_batch_count", 0)},
        {"check": "remote_interaction_session_count", "value": summary_dict.get("remote_interaction_session_count", 0)},
        {"check": "unknown_route_count", "value": summary_dict["unknown_route_count"]},
        {"check": "depot_slot_failure_count", "value": summary_dict["depot_slot_failure_count"]},
        {"check": "state_loop_count", "value": summary_dict["state_loop_count"]},
        {"check": "blocked_reason", "value": summary_dict["blocked_reason"] or "无"},
    ]
    st.dataframe(guard_rows, use_container_width=True, hide_index=True)
    st.caption(f"runtime 输入文件：{runtime_input_path}")
    if summary_dict.get("response_path"):
        st.caption(f"接口响应文件：{summary_dict['response_path']}")

    view = st.radio(
        "结果视图",
        options=["可视化回放", "批次/操作计划", "接口响应", "终态", "诊断"],
        horizontal=True,
        key="p10-result-view",
    )
    if view == "接口响应":
        st.caption("接口响应中的 Operations[].Index 是操作序号；业务勾数请以上方 Get/Put 计数为准。")
        st.json(response)
        st.download_button(
            "下载响应 JSON",
            data=json.dumps(response, ensure_ascii=False, indent=2),
            file_name=f"p10_response_{summary_dict['case_id']}.json",
            mime="application/json",
            key="p10-response-download",
        )
    elif view == "批次/操作计划":
        hook_rows = _p10_hook_summary_rows(operation_rows)
        if hook_rows:
            st.markdown("**按内部移动批次汇总**")
            st.dataframe(hook_rows, use_container_width=True, hide_index=True)
            st.markdown("**接口操作序列 / 业务挂摘勾号**")
            st.dataframe(_p10_operation_table_rows(operation_rows), use_container_width=True, hide_index=True)
        else:
            st.info("当前没有生成操作。")
    elif view == "可视化回放":
        _render_p10_replay(payload, operation_rows, response)
    elif view == "终态":
        _render_p10_end_status(response)
    else:
        _render_p10_diagnostics(summary, candidate_rows, rejection_reasons)


def _p10_status_text(status: str) -> str:
    return {
        "completed": "完成",
        "blocked": "阻塞",
        "invalid_input": "输入错误",
    }.get(status, status)


def _p10_format_blocked_reason(reason: str) -> str:
    if reason.startswith("target_final_capacity_infeasible:"):
        parts = reason.split(":")
        if len(parts) >= 3:
            return (
                f"{parts[1]} 终态容量不可行：目标车辆总长度 {parts[2].split('>')[0]}m "
                f"超过线路有效长度 {parts[2].split('>')[-1]}m。这个输入目标本身物理不可满足，"
                "不是可视化页面失败。"
            )
    if reason == "max_hook_limit_reached":
        return "达到最大内部移动批次限制后仍有车辆未满足目标，可调大批次限制或查看诊断中的候选拒绝原因。"
    if reason == "stagnant_no_progress":
        return "连续多钩没有减少未满足车辆，runtime 为避免循环主动停止。"
    if reason == "all_runtime_candidates_rejected":
        return "当前轮所有候选都被物理校验拒绝，请查看诊断中的 hardReasons。"
    if reason == "no_runtime_candidate_generated":
        return "当前状态没有生成可执行候选。"
    return reason


def _p10_business_hook_count(operation_rows) -> int:
    return sum(1 for row in operation_rows if row.action in P10_BUSINESS_HOOK_ACTIONS)


def _p10_action_counts(operation_rows) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for row in operation_rows:
        counts[str(row.action)] += 1
    return dict(counts)


def _p10_generated_status_from_payload(payload: dict) -> list[dict]:
    rows = []
    for car in payload.get("StartStatus") or []:
        rows.append(
            {
                "No": str(car.get("No") or ""),
                "Line": str(car.get("Line") or ""),
                "Position": _p10_int_or_zero(car.get("Position")),
            }
        )
    return sorted(rows, key=lambda row: (row["Line"], row["Position"], row["No"]))


def _p10_hook_summary_rows(operation_rows) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for hook_index, group in _p10_group_operations_by_hook(operation_rows).items():
        get_op = next((row for row in group if row.action == "Get"), group[0])
        put_op = next((row for row in reversed(group) if row.action == "Put"), group[-1])
        move_cars = _p10_split_pipe(get_op.move_cars)
        route: list[str] = []
        for op in group:
            route = _p10_extend_route(route, _p10_split_pipe(op.passby_path))
        rows.append(
            {
                "moveBatch": hook_index,
                "source": get_op.line,
                "target": put_op.line,
                "carCount": len(move_cars),
                "moveCars": " ".join(move_cars),
                "hasWeigh": any(row.action == "Weigh" for row in group),
                "businessHookCount": sum(row.action in P10_BUSINESS_HOOK_ACTIONS for row in group),
                "operationCount": len(group),
                "route": " -> ".join(route),
            }
        )
    return rows


def _p10_operation_table_rows(operation_rows) -> list[dict[str, object]]:
    rows = []
    business_hook_no = 0
    for row in sorted(operation_rows, key=lambda item: (item.hook_index, item.operation_index)):
        if row.action in P10_BUSINESS_HOOK_ACTIONS:
            business_hook_no += 1
            display_business_hook_no: int | str = business_hook_no
        else:
            display_business_hook_no = ""
        rows.append(
            {
                "businessHookNo": display_business_hook_no,
                "moveBatch": row.hook_index,
                "operationIndex": row.operation_index,
                "action": row.action,
                "line": row.line,
                "moveCars": " ".join(_p10_split_pipe(row.move_cars)),
                "trainCars": " ".join(_p10_split_pipe(row.train_cars)),
                "passbyPath": " -> ".join(_p10_split_pipe(row.passby_path)),
            }
        )
    return rows


def _p10_group_operations_by_hook(operation_rows) -> dict[int, list]:
    grouped: dict[int, list] = defaultdict(list)
    for row in operation_rows:
        grouped[int(row.hook_index)].append(row)
    return {
        hook_index: sorted(rows, key=lambda row: row.operation_index)
        for hook_index, rows in sorted(grouped.items())
    }


def _render_p10_replay(payload: dict, operation_rows, response: dict) -> None:
    frames = _p10_build_replay_frames(payload, operation_rows, response)
    if not frames:
        st.info("当前没有可回放状态。")
        return
    vehicle_target_tracks = _p10_vehicle_target_tracks(payload)
    max_frame_index = len(frames) - 1
    if "p10_replay_frame_index" not in st.session_state:
        st.session_state["p10_replay_frame_index"] = 0
    if int(st.session_state["p10_replay_frame_index"]) > max_frame_index:
        st.session_state["p10_replay_frame_index"] = max_frame_index
    st.markdown('<div id="p10-replay-anchor"></div>', unsafe_allow_html=True)
    frame_index = st.slider(
        "回放步骤（按接口操作推进）",
        min_value=0,
        max_value=max_frame_index,
        key="p10_replay_frame_index",
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
            key="p10-replay-view-mode",
        )
        if view_mode == "线路拓扑":
            st.markdown(
                _p10_topology_svg(
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
                _p10_yard_svg(
                    state_by_line=frame["state"],
                    active_path=set(frame["path"]),
                    source_line=frame["source_line"],
                    target_line=frame["target_line"],
                    move_cars=frame["move_cars"],
                    train_cars=frame["train_cars"],
                ),
                unsafe_allow_html=True,
            )
        st.markdown(
            _p10_route_chips_html(frame["path"]),
            unsafe_allow_html=True,
        )
    with side_col:
        side_rows = [
            {"label": "步骤", "value": f"{frame_index + 1}/{len(frames)}"},
            {"label": "业务勾号", "value": frame["business_hook"] or "无"},
            {"label": "内部移动批次", "value": frame["hook"] or "无"},
            {"label": "接口操作序号", "value": frame["operation"] or "无"},
            {"label": "动作", "value": frame["action"] or "初始"},
            {"label": "股道", "value": frame["active_line"] or "无"},
            {"label": "移动车辆", "value": " ".join(frame["move_cars"]) or "无"},
            {"label": "调车机后挂", "value": " ".join(frame["train_cars"]) or "无"},
        ]
        st.dataframe(side_rows, use_container_width=True, hide_index=True)

    state_rows = _p10_state_table_rows(
        frame["state"],
        highlighted_lines=set(frame["path"]) | {frame["source_line"], frame["target_line"]},
    )
    if state_rows:
        st.dataframe(state_rows, use_container_width=True, hide_index=True)


def _p10_build_replay_frames(payload: dict, operation_rows, response: dict) -> list[dict]:
    state = _p10_initial_state(payload)
    loco = payload.get("locoNode") or {}
    frames: list[dict] = [
        {
            "title": "初始状态",
            "detail": f"机车位置：{loco.get('Line') or '未知'} / {loco.get('End') or '未知'}",
            "state": _p10_copy_state(state),
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
    business_hook_no = 0
    for row in sorted(operation_rows, key=lambda item: (item.hook_index, item.operation_index)):
        action = row.action
        line = row.line
        move_cars = _p10_split_pipe(row.move_cars)
        path = _p10_split_pipe(row.passby_path)
        source_line = ""
        target_line = ""
        display_business_hook_no: int | str = ""

        if action == "Get":
            business_hook_no += 1
            display_business_hook_no = business_hook_no
            for car_no in move_cars:
                _p10_remove_car(state, car_no)
            train_cars = _p10_split_pipe(row.train_cars) or move_cars
            source_line = line
        elif action == "Weigh":
            train_cars = _p10_split_pipe(row.train_cars) or train_cars or move_cars
        elif action == "Put":
            business_hook_no += 1
            display_business_hook_no = business_hook_no
            for car_no in move_cars:
                _p10_remove_car(state, car_no)
            state.setdefault(line, [])
            existing = set(state[line])
            state[line].extend(car_no for car_no in move_cars if car_no not in existing)
            train_cars = []
            target_line = line

        frames.append(
            {
                "title": _p10_replay_frame_title(row, display_business_hook_no),
                "detail": f"{line} | 路径：{' -> '.join(path) if path else '无'}",
                "state": _p10_copy_state(state),
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
                "state": _p10_state_from_status(generated),
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


def _p10_replay_frame_title(row, business_hook_no: int | str) -> str:
    if business_hook_no:
        return (
            f"业务第 {business_hook_no} 勾 / 内部移动批次 {row.hook_index} / "
            f"接口操作 {row.operation_index}: {row.action}"
        )
    return f"内部移动批次 {row.hook_index} / 接口操作 {row.operation_index}: {row.action}"


def _p10_initial_state(payload: dict) -> dict[str, list[str]]:
    rows = []
    for car in payload.get("StartStatus") or []:
        no = str(car.get("No") or "").strip()
        line = str(car.get("Line") or "").strip()
        if no and line:
            rows.append((line, _p10_int_or_zero(car.get("Position")), no))
    state: dict[str, list[str]] = defaultdict(list)
    for line, _, no in sorted(rows):
        state[line].append(no)
    return dict(state)


def _p10_state_from_status(status_rows: list[dict]) -> dict[str, list[str]]:
    rows = []
    for item in status_rows:
        no = str(item.get("No") or "").strip()
        line = str(item.get("Line") or "").strip()
        if no and line:
            rows.append((line, _p10_int_or_zero(item.get("Position")), no))
    state: dict[str, list[str]] = defaultdict(list)
    for line, _, no in sorted(rows):
        state[line].append(no)
    return dict(state)


def _p10_copy_state(state: dict[str, list[str]]) -> dict[str, list[str]]:
    return {line: list(cars) for line, cars in state.items()}


def _p10_remove_car(state: dict[str, list[str]], car_no: str) -> None:
    for line in list(state):
        if car_no in state[line]:
            state[line] = [item for item in state[line] if item != car_no]


def _p10_vehicle_target_tracks(payload: dict) -> dict[str, set[str]]:
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


def _p10_line_target_counts(
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


def _p10_topology_svg(
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
    layout = _p10_schematic_layout()
    tracks = layout["tracks"]
    width = float(layout["canvas"]["width"])
    height = float(layout["canvas"]["height"])
    mainline_tracks = {item.get("trackCode") for item in layout.get("mainlineTracks", [])}
    active_tracks = set(_p10_expand_path_for_map(active_path, tracks))
    active_tracks.update(item for item in [source_line, target_line, active_line] if item in tracks)
    move_set = set(move_cars) | set(train_cars)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height:.0f}" class="p10-topology-svg">',
        "<style>",
        ".p10-topology-svg{width:100%;height:auto;background:#f8fafc;border:1px solid #d9e2ec;border-radius:8px;}",
        ".p10-track-line{fill:none;stroke:#cbd5e1;stroke-width:10;stroke-linecap:round;stroke-linejoin:round;}",
        ".p10-track-mainline{stroke:#94a3b8;stroke-width:10;}",
        ".p10-track-source{stroke:#f97316;stroke-width:14;}",
        ".p10-track-target{stroke:#059669;stroke-width:14;}",
        ".p10-track-label{font-family:PingFang SC,Arial,sans-serif;font-size:24px;font-weight:750;fill:#1f2937;text-anchor:middle;}",
        ".p10-track-label-muted{font-size:18px;fill:#64748b;}",
        ".p10-track-label-active{font-size:27px;fill:#1d4ed8;}",
        ".p10-route-overlay{fill:none;stroke:#2563eb;stroke-width:14;stroke-linecap:round;stroke-linejoin:round;}",
        ".p10-badge-ok{fill:#0f766e;stroke:#ffffff;stroke-width:2;}",
        ".p10-badge-pending{fill:#d97706;stroke:#ffffff;stroke-width:2;}",
        ".p10-badge-active{stroke:#1f2937;stroke-width:3;}",
        ".p10-badge-text{font-family:Arial,sans-serif;font-size:14px;font-weight:850;fill:#ffffff;text-anchor:middle;dominant-baseline:middle;}",
        ".p10-endpoint-source{fill:#fff7ed;stroke:#f97316;stroke-width:3;}",
        ".p10-endpoint-target{fill:#ecfdf5;stroke:#059669;stroke-width:3;}",
        ".p10-endpoint-text{font-family:PingFang SC,Arial,sans-serif;font-size:18px;font-weight:850;text-anchor:middle;dominant-baseline:middle;}",
        "</style>",
        f'<rect x="0" y="0" width="{width:.0f}" height="{height:.0f}" fill="#f8fafc" rx="8" />',
    ]

    endpoint_track_overlays: list[str] = []
    badge_overlays: list[str] = []
    for line, track in tracks.items():
        points = track.get("points") or []
        if len(points) < 2:
            continue
        css_class = "p10-track-line"
        if line in mainline_tracks:
            css_class += " p10-track-mainline"
        parts.append(f'<path class="{css_class}" d="{_p10_points_path(points)}" />')
        if line == source_line:
            endpoint_track_overlays.append(f'<path class="p10-track-line p10-track-source" d="{_p10_points_path(points)}" />')
        if line == target_line:
            endpoint_track_overlays.append(f'<path class="p10-track-line p10-track-target" d="{_p10_points_path(points)}" />')
        label = track.get("labelAnchor") or points[len(points) // 2]
        label_class = "p10-track-label"
        if line not in active_tracks and not track.get("alwaysVisible") and not state_by_line.get(line):
            label_class += " p10-track-label-muted"
        if line in active_tracks:
            label_class += " p10-track-label-active"
        if line in active_tracks or track.get("alwaysVisible") or state_by_line.get(line):
            parts.append(
                f'<text class="{label_class}" x="{float(label[0]):.1f}" y="{float(label[1]):.1f}">{escape(line)}</text>'
            )
        cars = state_by_line.get(line, [])
        if cars:
            ok_count, pending_count = _p10_line_target_counts(line, cars, vehicle_target_tracks)
            active_badge = any(car in move_set for car in cars)
            badge_overlays.append(
                _p10_track_count_badges_svg(
                    track,
                    ok_count=ok_count,
                    pending_count=pending_count,
                    active=active_badge,
                )
            )

    for item in _p10_expand_path_for_map(active_path, tracks):
        track = tracks.get(item)
        if not track:
            continue
        points = track.get("points") or []
        if len(points) >= 2:
            parts.append(f'<path class="p10-route-overlay" d="{_p10_points_path(points)}" />')

    parts.extend(endpoint_track_overlays)
    parts.extend(badge_overlays)

    for line, css_class, label in [
        (source_line, "p10-endpoint-source", "取"),
        (target_line, "p10-endpoint-target", "放"),
    ]:
        track = tracks.get(line)
        if not track:
            continue
        cx, cy = _p10_track_center(track)
        parts.append(f'<circle class="{css_class}" cx="{cx:.1f}" cy="{cy:.1f}" r="15" />')
        parts.append(f'<text class="p10-endpoint-text" x="{cx:.1f}" y="{cy + 1:.1f}">{label}</text>')

    parts.append("</svg>")
    return "".join(parts)


def _p10_schematic_layout() -> dict:
    return json.loads(P10_SCHEMATIC_LAYOUT_PATH.read_text(encoding="utf-8"))


def _p10_points_path(points: list[list[float]]) -> str:
    first = points[0]
    chunks = [f"M {float(first[0]):.1f} {float(first[1]):.1f}"]
    for point in points[1:]:
        chunks.append(f"L {float(point[0]):.1f} {float(point[1]):.1f}")
    return " ".join(chunks)


def _p10_track_center(track: dict) -> tuple[float, float]:
    points = track.get("points") or []
    if not points:
        return 0.0, 0.0
    return (
        sum(float(point[0]) for point in points) / len(points),
        sum(float(point[1]) for point in points) / len(points),
    )


def _p10_track_badge_anchor(track: dict) -> tuple[float, float]:
    label = track.get("labelAnchor")
    if label and len(label) >= 2:
        return float(label[0]) + 54.0, float(label[1]) - 18.0
    center_x, center_y = _p10_track_center(track)
    return center_x + 54.0, center_y - 18.0


def _p10_track_count_badges_svg(
    track: dict,
    *,
    ok_count: int,
    pending_count: int,
    active: bool,
) -> str:
    x, y = _p10_track_badge_anchor(track)
    active_class = " p10-badge-active" if active else ""
    parts: list[str] = []
    if ok_count > 0:
        parts.append(f'<circle class="p10-badge-ok{active_class}" cx="{x - 14:.1f}" cy="{y:.1f}" r="16" />')
        parts.append(f'<text class="p10-badge-text" x="{x - 14:.1f}" y="{y + 1:.1f}">{ok_count}</text>')
    if pending_count > 0:
        offset = 14 if ok_count > 0 else 0
        parts.append(f'<circle class="p10-badge-pending{active_class}" cx="{x + offset:.1f}" cy="{y:.1f}" r="16" />')
        parts.append(f'<text class="p10-badge-text" x="{x + offset:.1f}" y="{y + 1:.1f}">{pending_count}</text>')
    return "".join(parts)


def _p10_map_route_overlay_points(active_path: list[str], tracks: dict) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for item in _p10_expand_path_for_map(active_path, tracks):
        track = tracks.get(item)
        if not track:
            continue
        center = _p10_track_center(track)
        if not points or points[-1] != center:
            points.append(center)
    return points


def _p10_expand_path_for_map(active_path: list[str], tracks: dict) -> list[str]:
    edge_to_tracks = _p10_runtime_module().SWITCH_EDGE_TRACKS
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
        if index + 1 < len(active_path):
            mapped.extend(list(edge_to_tracks.get(frozenset((item, active_path[index + 1])), ())))
        for track in mapped:
            if track in tracks and (not expanded or expanded[-1] != track):
                expanded.append(track)
    return expanded


@lru_cache(maxsize=1)
def _p10_topology_model() -> dict[str, object]:
    p10 = _p10_runtime_module()
    nodes = {
        "L1": (80, 120),
        "L2": (235, 120),
        "L3": (125, 245),
        "L4": (285, 245),
        "L5": (245, 360),
        "L6": (375, 390),
        "L7": (470, 500),
        "L8": (640, 445),
        "L9": (760, 485),
        "L12": (520, 142),
        "L13": (650, 245),
        "L14": (765, 285),
        "L15": (875, 340),
        "L16": (995, 355),
        "L17": (1080, 315),
        "L18": (1160, 280),
        "L19": (1085, 430),
        "Z1": (470, 390),
        "Z2": (395, 335),
        "Z3": (445, 285),
        "Z4": (430, 190),
    }
    track_segments = _p10_named_track_segments()
    for line, spec in p10.TRACK_SPECS.items():
        if line in track_segments:
            continue
        attachments = p10.LINE_ATTACHMENTS.get(line) or ()
        anchor = nodes.get(attachments[0]) if attachments else None
        if anchor is None:
            continue
        x, y = anchor
        track_segments[line] = (x, y, x + 86, y - 38, x + 12, y - 48)
    return {
        "nodes": nodes,
        "switch_edges": [(left, right) for left, right, *_ in p10.SWITCH_EDGES],
        "track_segments": track_segments,
    }


def _p10_named_track_segments() -> dict[str, tuple[int, int, int, int, int, int]]:
    return {
        "存5线北": (235, 120, 330, 58, 255, 52),
        "存4线": (235, 120, 315, 92, 250, 84),
        "存3线": (285, 245, 380, 218, 305, 214),
        "存2线": (285, 245, 375, 280, 306, 305),
        "存1线": (245, 360, 140, 355, 122, 348),
        "存4南": (430, 190, 515, 210, 455, 232),
        "存5线南": (520, 142, 618, 142, 538, 128),
        "机北1": (125, 245, 72, 300, 36, 308),
        "机北2": (375, 390, 310, 442, 270, 454),
        "机库线": (470, 500, 545, 555, 492, 585),
        "调梁线北": (470, 500, 438, 585, 390, 607),
        "调梁棚": (470, 500, 560, 510, 490, 535),
        "机走北": (470, 390, 555, 360, 486, 352),
        "机走棚": (640, 445, 570, 410, 548, 405),
        "预修线": (650, 245, 600, 322, 560, 334),
        "洗油北": (640, 445, 708, 425, 676, 410),
        "机南": (765, 285, 720, 355, 690, 368),
        "洗罐线北": (760, 485, 850, 505, 782, 528),
        "洗罐站": (760, 485, 850, 455, 785, 448),
        "油漆线": (760, 485, 835, 550, 780, 575),
        "抛丸线": (875, 340, 900, 420, 876, 448),
        "卸轮线": (1085, 430, 1165, 460, 1110, 485),
        "修1库外": (1085, 430, 995, 490, 940, 514),
        "修1库内": (1085, 430, 1168, 515, 1108, 545),
        "修2库外": (1080, 315, 1000, 250, 945, 248),
        "修2库内": (1080, 315, 1165, 220, 1105, 206),
        "修3库外": (1160, 280, 1195, 205, 1142, 192),
        "修3库内": (1160, 280, 1210, 315, 1142, 342),
        "修4库外": (1160, 280, 1212, 382, 1142, 402),
        "修4库内": (1160, 280, 1215, 455, 1145, 476),
    }


def _p10_route_overlay_points(
    active_path: list[str],
    node_points: dict[str, tuple[int, int]],
    track_segments: dict[str, tuple[int, int, int, int, int, int]],
) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for item in active_path:
        if item in node_points:
            point = node_points[item]
        elif item in track_segments:
            x1, y1, x2, y2, _, _ = track_segments[item]
            point = ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
        else:
            continue
        if not points or points[-1] != point:
            points.append(point)
    return points


def _p10_yard_svg(
    *,
    state_by_line: dict[str, list[str]],
    active_path: set[str],
    source_line: str,
    target_line: str,
    move_cars: list[str],
    train_cars: list[str],
) -> str:
    groups = _p10_line_groups(state_by_line, active_path | {source_line, target_line})
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
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" class="p10-yard-svg">',
        "<style>",
        ".p10-yard-svg{width:100%;height:auto;background:#f8fafc;border:1px solid #d9e2ec;border-radius:8px;}",
        ".p10-yard-title{font-family:PingFang SC,Arial,sans-serif;font-size:15px;font-weight:700;fill:#1f2937;}",
        ".p10-line-label{font-family:PingFang SC,Arial,sans-serif;font-size:12px;font-weight:700;fill:#334155;}",
        ".p10-line-count{font-family:Arial,sans-serif;font-size:11px;font-weight:700;fill:#475569;text-anchor:end;}",
        ".p10-track{fill:#ffffff;stroke:#cbd5e1;stroke-width:1.4;}",
        ".p10-track-path{fill:#eff6ff;stroke:#2563eb;stroke-width:2.4;}",
        ".p10-track-source{fill:#fff7ed;stroke:#f97316;stroke-width:2.8;}",
        ".p10-track-target{fill:#ecfdf5;stroke:#059669;stroke-width:2.8;}",
        ".p10-chip{fill:#e2e8f0;stroke:#ffffff;stroke-width:1;}",
        ".p10-chip-active{fill:#0f766e;}",
        ".p10-chip-text{font-family:Arial,sans-serif;font-size:9px;font-weight:700;fill:#334155;text-anchor:middle;dominant-baseline:middle;}",
        ".p10-chip-text-active{fill:#ffffff;}",
        ".p10-more{font-family:Arial,sans-serif;font-size:10px;font-weight:700;fill:#64748b;}",
        "</style>",
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#f8fafc" rx="8" />',
    ]

    for group_index, (group_name, lines) in enumerate(groups):
        x = margin + group_index * (column_width + gap)
        parts.append(
            f'<text class="p10-yard-title" x="{x:.1f}" y="34">{escape(group_name)}</text>'
        )
        for row_index, line in enumerate(lines):
            y = 62 + row_index * row_height
            cars = state_by_line.get(line, [])
            track_x = x + 76
            track_y = y - 17
            track_width = column_width - 86
            css_class = "p10-track"
            if line in active_path:
                css_class = "p10-track p10-track-path"
            if line == source_line:
                css_class = "p10-track p10-track-source"
            if line == target_line:
                css_class = "p10-track p10-track-target"
            parts.append(f'<text class="p10-line-label" x="{x:.1f}" y="{y + 4:.1f}">{escape(line)}</text>')
            parts.append(
                f'<rect class="{css_class}" x="{track_x:.1f}" y="{track_y:.1f}" '
                f'width="{track_width:.1f}" height="24" rx="5" />'
            )
            parts.append(
                f'<text class="p10-line-count" x="{track_x + track_width - 7:.1f}" '
                f'y="{y + 3:.1f}">{len(cars)}</text>'
            )
            chip_x = track_x + 7
            max_chips = max(1, int((track_width - 48) // 36))
            for chip_index, car_no in enumerate(cars[:max_chips]):
                cx = chip_x + chip_index * 36
                is_active = car_no in move_set
                chip_class = "p10-chip p10-chip-active" if is_active else "p10-chip"
                text_class = "p10-chip-text p10-chip-text-active" if is_active else "p10-chip-text"
                label = car_no[-4:] if len(car_no) > 4 else car_no
                parts.append(
                    f'<rect class="{chip_class}" x="{cx:.1f}" y="{track_y + 4:.1f}" width="31" height="16" rx="4" />'
                )
                parts.append(
                    f'<text class="{text_class}" x="{cx + 15.5:.1f}" y="{track_y + 12.5:.1f}">{escape(label)}</text>'
                )
            if len(cars) > max_chips:
                parts.append(
                    f'<text class="p10-more" x="{chip_x + max_chips * 36 + 2:.1f}" '
                    f'y="{track_y + 16:.1f}">+{len(cars) - max_chips}</text>'
                )

    parts.append("</svg>")
    return "".join(parts)


def _p10_line_groups(state_by_line: dict[str, list[str]], active_lines: set[str]) -> list[tuple[str, list[str]]]:
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


def _p10_route_chips_html(path: list[str]) -> str:
    chips = path or ["无路径"]
    chip_html = "".join(f"<span class='p10-route-chip'>{escape(item)}</span>" for item in chips)
    return f"""
    <style>
    .p10-route-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin: 8px 0 12px 0;
    }}
    .p10-route-chip {{
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
    <div class="p10-route-row">{chip_html}</div>
    """


def _p10_state_table_rows(
    state_by_line: dict[str, list[str]],
    *,
    highlighted_lines: set[str],
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
                "cars": " ".join(cars),
            }
        )
    return rows


def _render_p10_end_status(response: dict) -> None:
    rows = _p10_end_status_rows(response)
    if not rows:
        st.info("接口响应中没有 GeneratedEndStatus。")
        return
    line_rows = []
    grouped: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        grouped[str(row["line"])].append(str(row["vehicleNo"]))
    for line, cars in sorted(grouped.items()):
        line_rows.append({"line": line, "carCount": len(cars), "cars": " ".join(cars)})
    st.markdown("**终态股道汇总**")
    st.dataframe(line_rows, use_container_width=True, hide_index=True)
    st.markdown("**终态车辆位置**")
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _p10_end_status_rows(response: dict) -> list[dict[str, object]]:
    status_rows = ((response or {}).get("Data") or {}).get("GeneratedEndStatus") or []
    rows = [
        {
            "vehicleNo": str(item.get("No") or ""),
            "line": str(item.get("Line") or ""),
            "position": _p10_int_or_zero(item.get("Position")),
        }
        for item in status_rows
    ]
    return sorted(rows, key=lambda row: (row["line"], row["position"], row["vehicleNo"]))


def _render_p10_diagnostics(summary, candidate_rows, rejection_reasons) -> None:
    st.markdown("**CaseSummaryRow**")
    st.json(asdict(summary))

    reason_rows = [
        {"reason": reason, "count": count}
        for reason, count in sorted(rejection_reasons.items(), key=lambda item: (-item[1], item[0]))
    ]
    if reason_rows:
        st.markdown("**拒绝/阻塞原因分布**")
        st.dataframe(reason_rows, use_container_width=True, hide_index=True)

    candidate_dicts = [asdict(row) for row in candidate_rows]
    if not candidate_dicts:
        st.info("当前没有候选审计记录。")
        return
    statuses = sorted({row["candidate_status"] for row in candidate_dicts})
    status_filter = st.selectbox(
        "候选状态",
        options=["全部", *statuses],
        key="p10-candidate-status-filter",
    )
    filtered = [
        row
        for row in candidate_dicts
        if status_filter == "全部" or row["candidate_status"] == status_filter
    ]
    display_rows = [
        {
            "hook": row["hook_index"],
            "status": row["candidate_status"],
            "source": row["source_line"],
            "target": row["target_line"],
            "actionFamily": row["action_family"],
            "carCount": row["move_car_count"],
            "moveCars": row["move_cars"].replace("|", " "),
            "hardViolationCount": row["hard_violation_count"],
            "hardReasons": row["hard_violation_reasons"],
            "getRoute": row["get_route_exists"],
            "putRoute": row["put_route_exists"],
            "generationReason": row["generation_reason"],
        }
        for row in filtered
    ]
    st.caption(f"当前显示 {len(display_rows)} / {len(candidate_dicts)} 条候选记录。")
    st.dataframe(display_rows, use_container_width=True, hide_index=True)


def _p10_split_pipe(text: str) -> list[str]:
    return [item for item in str(text or "").split("|") if item]


def _p10_extend_route(route: list[str], segment: list[str]) -> list[str]:
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


def _p10_int_or_zero(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _render_single_scenario_page() -> None:
    st.subheader("案例回放")
    if _FZED_IMPORT_ERROR is not None:
        st.warning(f"当前环境缺少旧案例回放依赖 fzed_shunting：{_FZED_IMPORT_ERROR}")
        st.caption("P10 求解演示不依赖该包，可以在第一个 Tab 直接运行。")
        return

    scenario_path = st.text_input("Scenario JSON 路径", value="")
    auto_solver = st.checkbox(
        "使用全量验证同款求解参数（推荐）",
        value=True,
        help="开启后使用 full validation 默认口径：beam、beam_width=8、60s 外层超时约 55s solver 预算。关闭则手动指定 Solver。",
    )
    if auto_solver:
        timeout_seconds = st.number_input(
            "超时（秒）",
            min_value=1.0,
            max_value=600.0,
            value=VALIDATION_DEFAULT_TIMEOUT_SECONDS,
            step=1.0,
            help="对齐 scripts/run_external_validation_parallel.py 的 60s 口径；内部 solver 预算会预留约 5s 页面/校验余量。",
        )
        solver = VALIDATION_DEFAULT_SOLVER
        heuristic_weight = 1.0
        beam_width = VALIDATION_DEFAULT_BEAM_WIDTH
        time_budget_ms: float | None = _validation_time_budget_ms(float(timeout_seconds))
    else:
        solver = st.selectbox("Solver", options=["beam", "exact", "weighted", "real_hook", "lns"], index=0)
        heuristic_weight = st.number_input("Heuristic Weight", min_value=1.0, value=1.0, step=0.5)
        beam_width = st.number_input("Beam Width", min_value=1, value=VALIDATION_DEFAULT_BEAM_WIDTH, step=1)
        time_budget_ms = None
    if not scenario_path:
        st.info("先通过 CLI 生成一个 scenario.json，或使用 `artifacts/typical_suite.json` 中的典型场景，再粘贴路径。")
        return

    path = Path(scenario_path)
    if not path.exists():
        st.error("文件不存在")
        return

    master = _get_master_data()
    raw_payload = json.loads(path.read_text(encoding="utf-8"))
    selected_payload, scenario_names, active_scenario_name = select_demo_payload(raw_payload)
    payload = selected_payload
    if scenario_names:
        selected_name = st.selectbox(
            "场景",
            options=scenario_names,
            index=scenario_names.index(active_scenario_name) if active_scenario_name else 0,
        )
        payload, _, active_scenario_name = select_demo_payload(raw_payload, selected_name=selected_name)
        scenario_meta = next(
            (
                item
                for item in raw_payload.get("scenarios", [])
                if item.get("name") == active_scenario_name
            ),
            None,
        )
        if scenario_meta and scenario_meta.get("description"):
            st.caption(str(scenario_meta["description"]))
    _render_workflow_demo(
        master=master,
        payload=_as_l7_workflow_payload(payload),
        solver=solver,
        heuristic_weight=heuristic_weight,
        beam_width=beam_width if solver in {"beam", "lns"} else None,
        time_budget_ms=time_budget_ms,
    )
    return


def _render_evaluation_dashboard() -> None:
    st.subheader("评估统计")
    default_path = str(DEFAULT_EVAL_ARTIFACT if DEFAULT_EVAL_ARTIFACT.exists() else "")
    artifact_path = st.text_input(
        "评估 JSON 路径",
        value=default_path,
        help="例如 artifacts/l7_phase1234_truth_phase3_tail_run_preflight_20260614.json",
    )
    if not artifact_path:
        st.info("先填写评估 JSON 路径。")
        return
    path = Path(artifact_path)
    if not path.exists():
        st.warning("评估 JSON 还不存在；如果正在跑全量评估，等进程结束后刷新。")
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        st.error(f"评估 JSON 读取失败：{exc}")
        return
    dataset_name, dataset = _extract_eval_dataset(payload)
    rows = list(dataset.get("rows") or [])
    if not rows:
        st.warning("评估 JSON 中没有 rows。")
        return

    st.caption(f"数据集：{dataset_name} | 文件：{path}")
    summary_cols = st.columns(7)
    summary_cols[0].metric("样本数", dataset.get("scenario_count", len(rows)))
    summary_cols[1].metric("Phase1 可解", dataset.get("phase1_ok_count", "-"))
    summary_cols[2].metric("Phase2 可解", dataset.get("phase2_ok_count", "-"))
    summary_cols[3].metric("可进 Phase3", dataset.get("phase2_can_enter_phase3_count", "-"))
    summary_cols[4].metric("Phase3 可解", dataset.get("phase3_ok_count", "-"))
    summary_cols[5].metric("Phase4 可解", dataset.get("phase4_ok_count", "-"))
    summary_cols[6].metric("1-4 全通", dataset.get("phase1234_ok_count", "-"))

    stage_rows = _build_eval_stage_summary_rows(dataset, rows)
    st.markdown("**阶段分布**")
    st.dataframe(stage_rows, use_container_width=True, hide_index=True)

    failed_distribution = dataset.get("failed_stage_distribution") or {}
    if failed_distribution:
        st.markdown("**失败阶段分布**")
        st.dataframe(
            [{"failedAt": key, "count": value} for key, value in sorted(failed_distribution.items())],
            use_container_width=True,
            hide_index=True,
        )

    case_rows = _build_eval_case_rows(rows)
    failed_options = ["全部", *sorted({str(row["failedAt"]) for row in case_rows})]
    solved_options = ["全部", "Phase1 可解", "Phase2 可解", "Phase3 可解", "Phase4 可解", "1-4 全通", "未全通"]
    filter_cols = st.columns([2, 2, 2])
    failed_filter = filter_cols[0].selectbox("失败阶段", failed_options)
    solved_filter = filter_cols[1].selectbox("阶段筛选", solved_options)
    min_phase3_hooks = filter_cols[2].number_input("Phase3 最小钩数", min_value=0, value=0, step=1)
    filtered_rows = _filter_eval_case_rows(
        case_rows,
        failed_filter=failed_filter,
        solved_filter=solved_filter,
        min_phase3_hooks=int(min_phase3_hooks),
    )
    st.markdown("**案例明细**")
    st.caption(f"当前显示 {len(filtered_rows)} / {len(case_rows)} 个案例。")
    st.dataframe(filtered_rows, use_container_width=True, hide_index=True)
    st.download_button(
        "下载当前明细 CSV",
        data=_rows_to_csv(filtered_rows),
        file_name="l7_eval_case_rows.csv",
        mime="text/csv",
    )

    scenario_names = [str(row["scenario"]) for row in filtered_rows]
    if not scenario_names:
        return
    selected_scenario = st.selectbox("选中案例", scenario_names)
    selected_path = Path(__file__).resolve().parent / "data" / "validation_inputs" / dataset_name / selected_scenario
    st.code(str(selected_path), language="text")
    if st.checkbox("在本页回放选中案例", value=False):
        if not selected_path.exists():
            st.error("选中案例文件不存在。")
            return
        raw_payload = json.loads(selected_path.read_text(encoding="utf-8"))
        _render_workflow_demo(
            master=_get_master_data(),
            payload=_as_l7_workflow_payload(raw_payload),
            solver=VALIDATION_DEFAULT_SOLVER,
            heuristic_weight=1.0,
            beam_width=VALIDATION_DEFAULT_BEAM_WIDTH,
            time_budget_ms=_validation_time_budget_ms(VALIDATION_DEFAULT_TIMEOUT_SECONDS),
        )


def _extract_eval_dataset(payload: dict) -> tuple[str, dict]:
    if "truth" in payload and isinstance(payload["truth"], dict):
        return "truth", dict(payload["truth"])
    if len(payload) == 1:
        name = next(iter(payload))
        value = payload[name]
        if isinstance(value, dict):
            return str(name), dict(value)
    return "truth", payload


def _build_eval_stage_summary_rows(dataset: dict, rows: list[dict]) -> list[dict]:
    result: list[dict] = []
    for stage_no in range(1, 5):
        hook_key = f"stage{stage_no}_hook_distribution"
        hooks = dataset.get(hook_key) or _distribution([
            _as_float((row.get(f"phase{stage_no}Actual") or {}).get("hookCount"))
            for row in rows
        ])
        elapsed_values = [
            _stage_elapsed_ms(row, stage_no)
            for row in rows
            if _stage_prefix(row, stage_no).get("elapsed_ms") is not None
        ]
        elapsed = _distribution(elapsed_values)
        result.append(
            {
                "stage": f"Phase{stage_no}",
                "prefixOk": sum(1 for row in rows if _stage_prefix(row, stage_no).get("ok") is True),
                "actualValid": sum(1 for row in rows if (row.get(f"phase{stage_no}Actual") or {}).get("isValid") is True),
                "hookP50": hooks.get("p50"),
                "hookP90": hooks.get("p90"),
                "hookP95": hooks.get("p95"),
                "hookMax": hooks.get("max"),
                "elapsedP50Ms": elapsed.get("p50"),
                "elapsedP90Ms": elapsed.get("p90"),
                "elapsedP95Ms": elapsed.get("p95"),
                "elapsedMaxMs": elapsed.get("max"),
            }
        )
    return result


def _build_eval_case_rows(rows: list[dict]) -> list[dict]:
    case_rows: list[dict] = []
    for row in rows:
        item = {
            "scenario": str(row.get("scenario") or ""),
            "failedAt": str(row.get("failedAt") or ""),
            "solved123": bool(row.get("solved123")),
            "solved1234": bool(row.get("solved1234")),
            "totalElapsedMs": _round_or_none(_as_float(row.get("elapsed_ms"))),
        }
        for stage_no in range(1, 5):
            prefix = _stage_prefix(row, stage_no)
            actual = row.get(f"phase{stage_no}Actual") or {}
            item[f"phase{stage_no}Ok"] = prefix.get("ok") is True
            item[f"phase{stage_no}Valid"] = actual.get("isValid") is True
            item[f"phase{stage_no}Hooks"] = _hook_count(row, stage_no)
            item[f"phase{stage_no}ElapsedMs"] = _round_or_none(_stage_elapsed_ms(row, stage_no))
        case_rows.append(item)
    return sorted(
        case_rows,
        key=lambda item: (
            bool(item.get("solved1234")),
            bool(item.get("solved123")),
            -float(item.get("phase3Hooks") or 0),
            str(item.get("scenario") or ""),
        ),
    )


def _filter_eval_case_rows(
    rows: list[dict],
    *,
    failed_filter: str,
    solved_filter: str,
    min_phase3_hooks: int,
) -> list[dict]:
    result = []
    for row in rows:
        if failed_filter != "全部" and row.get("failedAt") != failed_filter:
            continue
        if solved_filter == "Phase1 可解" and not row.get("phase1Ok"):
            continue
        if solved_filter == "Phase2 可解" and not row.get("phase2Ok"):
            continue
        if solved_filter == "Phase3 可解" and not row.get("phase3Ok"):
            continue
        if solved_filter == "Phase4 可解" and not row.get("phase4Ok"):
            continue
        if solved_filter == "1-4 全通" and not row.get("solved1234"):
            continue
        if solved_filter == "未全通" and row.get("solved1234"):
            continue
        if int(row.get("phase3Hooks") or 0) < min_phase3_hooks:
            continue
        result.append(row)
    return result


def _stage_prefix(row: dict, stage_no: int) -> dict:
    return row.get(f"phase{stage_no}Prefix") or {}


def _hook_count(row: dict, stage_no: int) -> int | None:
    actual = row.get(f"phase{stage_no}Actual") or {}
    parsed = _as_float(actual.get("hookCount"))
    if parsed is not None:
        return int(parsed)
    counts = _stage_prefix(row, stage_no).get("stage_hook_counts") or []
    if len(counts) >= stage_no:
        return int(counts[stage_no - 1])
    return None


def _stage_elapsed_ms(row: dict, stage_no: int) -> float | None:
    current = _as_float(_stage_prefix(row, stage_no).get("elapsed_ms"))
    if current is None:
        return None
    if stage_no == 1:
        return current
    previous = _as_float(_stage_prefix(row, stage_no - 1).get("elapsed_ms"))
    if previous is None:
        return current
    return max(0.0, current - previous)


def _distribution(raw_values: list[float | None]) -> dict[str, float | None]:
    values = sorted(value for value in raw_values if value is not None)
    if not values:
        return {"min": None, "p50": None, "p90": None, "p95": None, "max": None, "avg": None}
    return {
        "min": _round_or_none(values[0]),
        "p50": _round_or_none(_percentile(values, 50)),
        "p90": _round_or_none(_percentile(values, 90)),
        "p95": _round_or_none(_percentile(values, 95)),
        "max": _round_or_none(values[-1]),
        "avg": _round_or_none(sum(values) / len(values)),
    }


def _percentile(values: list[float], percentile: float) -> float:
    if len(values) == 1:
        return values[0]
    rank = (len(values) - 1) * percentile / 100.0
    lower = int(rank)
    upper = min(lower + 1, len(values) - 1)
    weight = rank - lower
    return values[lower] * (1.0 - weight) + values[upper] * weight


def _as_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_or_none(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 3)


def _rows_to_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    columns = list(rows[0].keys())
    csv_rows = [",".join(columns)]
    for row in rows:
        csv_rows.append(",".join(_csv_cell(row.get(column)) for column in columns))
    return "\n".join(csv_rows) + "\n"


def _csv_cell(value) -> str:
    text = "" if value is None else str(value)
    if any(ch in text for ch in [",", "\"", "\n"]):
        text = "\"" + text.replace("\"", "\"\"") + "\""
    return text


def _render_workflow_demo(
    master,
    payload,
    solver: str,
    heuristic_weight: float,
    beam_width: int | None,
    time_budget_ms: float | None = None,
):
    with st.spinner("求解中…"):
        workflow_view = _cached_workflow_view_model(
            _payload_cache_key(payload),
            solver,
            heuristic_weight,
            beam_width,
            time_budget_ms,
        )
    workflow = workflow_view.workflow
    st.subheader("多轮 Workflow")
    st.caption(f"共 {workflow.stage_count} 个阶段。")
    if workflow_view.failure:
        _render_workflow_failure(workflow_view.failure)
    if not workflow.stages:
        st.info("尚无已完成阶段可回放。")
        return
    stage_names = [stage.name for stage in workflow.stages]
    stage_index = st.selectbox(
        "阶段",
        options=list(range(len(stage_names))),
        index=0,
        format_func=lambda idx: stage_names[idx],
    )
    st.progress(_workflow_progress_value(stage_index=stage_index, stage_count=workflow.stage_count))
    st.dataframe(
        _build_workflow_stage_rows(workflow),
        use_container_width=True,
        hide_index=True,
    )
    st.dataframe(
        _build_workflow_transition_rows(workflow),
        use_container_width=True,
        hide_index=True,
    )
    stage = workflow.stages[stage_index]
    if stage.description:
        st.caption(stage.description)
    view = stage.view
    if view is None:
        st.error("阶段无结果")
        return
    vehicle_display_metadata = _build_vehicle_display_metadata(stage.input_payload)

    summary_cols = st.columns(6)
    summary_cols[0].metric("阶段车辆数", view.summary.vehicle_count)
    summary_cols[1].metric("阶段钩数", view.summary.hook_count)
    summary_cols[2].metric("已称重车辆", view.summary.weighed_vehicle_count)
    summary_cols[3].metric("最终占用线", len(view.summary.final_tracks))
    summary_cols[4].metric("库内台位", view.summary.assigned_spot_count)
    summary_cols[5].metric("Verifier", "PASS" if view.summary.is_valid else "FAIL")
    st.caption("当前 PASS/FAIL 验证的是本 workflow 阶段当前 Solver 生成的钩计划。")

    if view.verifier_errors:
        st.error("阶段校验未通过")
        st.json(view.verifier_errors)

    st.markdown("**阶段钩计划**")
    st.dataframe(
        [
            {
                "hookNo": hook.hook_no,
                "actionType": hook.action_type,
                "sourceTrack": hook.source_track,
                "targetTrack": hook.target_track,
                "vehicleCount": hook.vehicle_count,
                "vehicleNos": " ".join(hook.vehicle_nos),
                "pathTracks": " -> ".join(hook.path_tracks),
                "routeLengthM": hook.route_length_m,
                "remark": hook.remark,
            }
            for hook in view.hook_plan
        ],
        use_container_width=True,
        hide_index=True,
    )

    step_index = st.slider(
        "阶段 Step",
        min_value=0,
        max_value=len(view.steps) - 1,
        value=0,
        key=f"workflow-step-{stage_index}",
    )
    _render_step(view, step_index, vehicle_display_metadata=vehicle_display_metadata)


def _render_workflow_failure(failure: dict[str, object]) -> None:
    failed_index = failure.get("failedStageIndex")
    total_count = failure.get("totalStageCount")
    failed_name = failure.get("failedStageName")
    cause = failure.get("causeMessage")
    st.error(f"Workflow 在第 {failed_index}/{total_count} 阶段失败：{failed_name}")
    if cause:
        st.caption(str(cause))
    stage_input_summary = failure.get("stageInputSummary")
    if isinstance(stage_input_summary, dict) and stage_input_summary:
        with st.expander("失败阶段输入摘要", expanded=True):
            st.json(stage_input_summary)


def _is_workflow_payload(payload: dict) -> bool:
    return isinstance(payload.get("workflowStages"), list) or is_l7_closed_topology_mode(payload)


def _render_step(view, step_index: int, *, vehicle_display_metadata: dict[str, dict[str, str]] | None = None):
    step = view.steps[step_index]
    vehicle_meta = vehicle_display_metadata or {}
    if step.hook is None:
        st.info("Step 0 为初始状态。")
    else:
        st.write(f"第 {step.hook.hook_no} 钩: {_hook_title(step.hook)}")
        st.caption(_format_pre_hook_loco_carry_text(view, step_index, vehicle_meta))
        st.caption(
            f"车辆: {_format_hook_vehicle_text(step.hook.vehicle_nos, vehicle_meta)} | "
            f"路径: {' -> '.join(step.hook.path_tracks)}"
        )
        if step.hook.remark:
            st.caption(step.hook.remark)
        if step.verifier_errors:
            st.error(step.verifier_errors)

    transition_frame = None
    canvas_col, sidebar_col = st.columns([7, 3])
    with canvas_col:
        st.markdown("**方位示意回放**")
        if step.transition_frames:
            frame_index = st.slider(
                "钩内动画帧",
                min_value=0,
                max_value=len(step.transition_frames) - 1,
                value=len(step.transition_frames) - 1,
                key=f"transition-frame-{step.step_index}",
            )
            transition_frame = step.transition_frames[frame_index]
        _render_topology_graph(step.topology_graph, step.track_map, hook=step.hook, transition_frame=transition_frame, spot_assignments=step.spot_assignments, vehicle_target_tracks=view.vehicle_target_tracks)
    with sidebar_col:
        st.markdown("**当前钩摘要**")
        _render_hook_sidebar(step, vehicle_target_tracks=view.vehicle_target_tracks)

    detail_tabs = st.tabs(["股道变化", "车辆明细", "校验结果", "本钩路径距离"])
    with detail_tabs[0]:
        rows = _build_step_state_rows(step.track_map, vehicle_meta)
        if rows:
            st.dataframe(rows, use_container_width=True, hide_index=True)
        else:
            st.caption("当前无需要关注的股道状态。")
    with detail_tabs[1]:
        _render_vehicle_detail_panel(step, view, vehicle_meta)
    with detail_tabs[2]:
        _render_verifier_panel(step, view)
    with detail_tabs[3]:
        rows = _build_distance_breakdown_rows(step.hook)
        if rows:
            st.dataframe(rows, use_container_width=True, hide_index=True)
        else:
            st.caption("初始状态无路径距离构成。")


def _render_topology_graph(topology_graph, track_map, hook=None, transition_frame=None, spot_assignments=None, vehicle_target_tracks=None):
    st.markdown(
        _build_topology_svg(
            topology_graph,
            track_map,
            hook=hook,
            transition_frame=transition_frame,
            spot_assignments=spot_assignments,
            vehicle_target_tracks=vehicle_target_tracks,
        ),
        unsafe_allow_html=True,
    )


def _build_topology_graph_dot(topology_graph, track_map) -> str:
    lines = [
        "graph shunting_topology {",
        '  graph [bgcolor="transparent", pad="0.3", nodesep="0.35", ranksep="0.55"];',
        '  node [shape=box, style="rounded,filled", fontname="PingFang SC", fontsize=11, color="#d8d1c2", penwidth=1.0];',
        '  edge [fontname="PingFang SC", color="#c5c0b5", penwidth=1.6];',
    ]
    active_edges = set(topology_graph.active_edge_keys)
    changed_tracks = set(track_map.changed_tracks)
    active_tracks = set(track_map.active_path_tracks)
    for track_code, node in topology_graph.nodes.items():
        track_state = track_map.track_nodes.get(track_code)
        if track_state is None:
            continue
        fill = "#f0ebe2"
        border = "#d8d1c2"
        if track_state.has_loco:
            fill = "#fde7cf"
            border = "#d8772a"
        elif track_state.is_in_active_path:
            fill = "#dcf0ec"
            border = "#1d6f6d"
        elif track_state.is_occupied:
            fill = "#f7f2ea"
            border = "#8f8574"
        if track_state.is_changed:
            border = "#d8772a"
        status_parts: list[str] = []
        if track_state.has_loco:
            status_parts.append("机车")
        status_parts.append(f"占用 {len(track_state.vehicle_nos)}" if track_state.is_occupied else "空")
        label = f'{track_code}\\n{" / ".join(status_parts)}'
        lines.append(
            f'  "{track_code}" [label="{label}", fillcolor="{fill}", color="{border}"];'
        )
    for left, right in topology_graph.edge_keys:
        edge_color = "#c5c0b5"
        penwidth = 1.6
        if (left, right) in active_edges or (right, left) in active_edges:
            edge_color = "#1d6f6d"
            penwidth = 3.0
        elif left in changed_tracks or right in changed_tracks:
            edge_color = "#d8772a"
            penwidth = 2.2
        lines.append(
            f'  "{left}" -- "{right}" [color="{edge_color}", penwidth={penwidth}];'
        )
    lines.append("}")
    return "\n".join(lines)


def _build_topology_svg(
    topology_graph,
    track_map,
    hook=None,
    transition_frame=None,
    animate: bool = False,
    show_all_labels: bool = False,
    spot_assignments: dict | None = None,
    vehicle_target_tracks: dict | None = None,
) -> str:
    layout = _get_schematic_layout()
    track_nodes = track_map.track_nodes if track_map is not None else {}
    active_tracks = set(track_map.active_path_tracks) if track_map is not None else set()
    changed_tracks = set(track_map.changed_tracks) if track_map is not None else set()
    occupied_tracks = {
        track_code
        for track_code, node in track_nodes.items()
        if node.is_occupied or node.has_loco
    }
    source_track = hook.source_track if hook is not None else None
    target_track = hook.target_track if hook is not None else None
    if source_track is not None:
        active_tracks.add(source_track)
    if target_track is not None:
        active_tracks.add(target_track)

    route = build_schematic_route(layout, hook.path_tracks if hook is not None else [])
    motion_path = route_to_svg_path(route)

    parts = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 '
            f'{layout.canvas_width:.0f} {layout.canvas_height:.0f}" '
            'class="topology-svg">'
        ),
        "<style>",
        ".schematic-bg{fill:#fcfaf5;}",
        ".schematic-area{fill:#f4f0e6;stroke:#ddd4c1;stroke-width:1.5;}",
        ".schematic-area-label{font-family:PingFang SC,sans-serif;font-size:24px;font-weight:700;fill:#8c816d;text-anchor:middle;}",
        ".schematic-track{fill:none;stroke:#cbc3b3;stroke-width:10;stroke-linecap:round;stroke-linejoin:round;}",
        ".schematic-track-mainline{stroke:#b7ae9d;stroke-width:11;}",
        ".schematic-track-active{fill:none;stroke:#0f766e;stroke-width:14;stroke-linecap:round;stroke-linejoin:round;}",
        ".schematic-track-changed{fill:none;stroke:#d97706;stroke-width:12;stroke-linecap:round;stroke-linejoin:round;stroke-dasharray:10 8;}",
        ".schematic-track-label{font-family:PingFang SC,sans-serif;font-size:16px;fill:#574f44;text-anchor:middle;}",
        ".schematic-track-label-active{font-family:PingFang SC,sans-serif;font-size:18px;font-weight:700;fill:#0f4f4a;text-anchor:middle;}",
        ".schematic-track-label-key{font-family:PingFang SC,sans-serif;font-size:16px;font-weight:600;fill:#6b5f4b;text-anchor:middle;}",
        ".schematic-badge{fill:#fffaf0;stroke:#d9cdb5;stroke-width:1.5;}",
        ".schematic-badge-text{font-family:PingFang SC,sans-serif;font-size:12px;font-weight:700;fill:#6a5d45;text-anchor:middle;dominant-baseline:middle;}",
        ".sb-parked{fill:#0f766e;stroke:#ffffff;stroke-width:1.5;}",
        ".sb-parked-txt{font-family:PingFang SC,sans-serif;font-size:11px;font-weight:700;fill:#ffffff;text-anchor:middle;dominant-baseline:middle;}",
        ".sb-transit{fill:#d97706;stroke:#ffffff;stroke-width:1.5;}",
        ".sb-transit-txt{font-family:PingFang SC,sans-serif;font-size:11px;font-weight:700;fill:#ffffff;text-anchor:middle;dominant-baseline:middle;}",
        ".schematic-endpoint{fill:#fdf7ed;stroke:#0f766e;stroke-width:3;}",
        ".schematic-endpoint-target{fill:#fff2e0;stroke:#d97706;stroke-width:3;}",
        ".schematic-endpoint-text{font-family:PingFang SC,sans-serif;font-size:12px;font-weight:700;text-anchor:middle;dominant-baseline:middle;}",
        ".moving-block-marker{fill:#0f766e;stroke:#ffffff;stroke-width:3;}",
        ".moving-block-label{font-family:PingFang SC,sans-serif;font-size:22px;fill:#ffffff;text-anchor:middle;dominant-baseline:middle;}",
        ".loco-marker{fill:#d97706;stroke:#ffffff;stroke-width:3;}",
        ".route-motion-path{fill:none;stroke:none;}",
        "</style>",
        f'<rect class="schematic-bg" x="0" y="0" width="{layout.canvas_width:.1f}" height="{layout.canvas_height:.1f}" />',
    ]

    for area in layout.areas:
        parts.append(
            f'<rect class="schematic-area" x="{area.x:.1f}" y="{area.y:.1f}" width="{area.width:.1f}" '
            f'height="{area.height:.1f}" rx="24" />'
        )
        parts.append(
            f'<text class="schematic-area-label" x="{area.center.x:.1f}" y="{area.y + 34:.1f}">{escape(area.label)}</text>'
        )

    for geometry in layout.track_geometries.values():
        if geometry.track_code in active_tracks:
            parts.append(f'<path class="schematic-track-active" d="{_track_polyline_to_svg(geometry.points)}" />')
        else:
            base_class = "schematic-track"
            if geometry.is_mainline:
                base_class += " schematic-track-mainline"
            parts.append(f'<path class="{base_class}" d="{_track_polyline_to_svg(geometry.points)}" />')

    if motion_path:
        parts.append(f'<path id="route-motion-path" class="route-motion-path" d="{motion_path}" />')

    for track_code in sorted(changed_tracks - set(hook.path_tracks if hook is not None else [])):
        geometry = layout.track_geometries.get(track_code)
        if geometry is None:
            continue
        parts.append(f'<path class="schematic-track-changed" d="{_track_polyline_to_svg(geometry.points)}" />')

    visible_labels = {
        track_code
        for track_code, geometry in layout.track_geometries.items()
        if geometry.always_visible or show_all_labels
    }
    visible_labels.update(active_tracks)
    visible_labels.update(changed_tracks)
    visible_labels.update(occupied_tracks)

    for track_code in sorted(visible_labels):
        geometry = layout.track_geometries.get(track_code)
        if geometry is None:
            continue
        label_class = "schematic-track-label"
        if geometry.always_visible and track_code not in active_tracks and track_code not in changed_tracks:
            label_class = "schematic-track-label-key"
        if track_code in active_tracks or track_code in changed_tracks:
            label_class = "schematic-track-label-active"
        parts.append(
            f'<text class="{label_class}" x="{geometry.label_anchor.x:.1f}" y="{geometry.label_anchor.y:.1f}">{escape(track_code)}</text>'
        )

    vtt = vehicle_target_tracks or {}
    for track_code in sorted(occupied_tracks):
        geometry = layout.track_geometries.get(track_code)
        node = track_nodes.get(track_code)
        if geometry is None or node is None or not node.vehicle_nos:
            continue
        in_place = sum(1 for v in node.vehicle_nos if track_code in vtt.get(v, []))
        not_in_place = len(node.vehicle_nos) - in_place
        bx = geometry.label_anchor.x + 34.0
        by = geometry.label_anchor.y - 12.0
        if in_place > 0 and not_in_place > 0:
            parts.append(f'<circle class="sb-parked" cx="{bx - 11:.1f}" cy="{by:.1f}" r="10" />')
            parts.append(f'<text class="sb-parked-txt" x="{bx - 11:.1f}" y="{by + 1:.1f}">{in_place}</text>')
            parts.append(f'<circle class="sb-transit" cx="{bx + 11:.1f}" cy="{by:.1f}" r="10" />')
            parts.append(f'<text class="sb-transit-txt" x="{bx + 11:.1f}" y="{by + 1:.1f}">{not_in_place}</text>')
        elif in_place > 0:
            parts.append(f'<circle class="sb-parked" cx="{bx:.1f}" cy="{by:.1f}" r="12" />')
            parts.append(f'<text class="sb-parked-txt" x="{bx:.1f}" y="{by + 1:.1f}">{in_place}</text>')
        else:
            parts.append(f'<circle class="sb-transit" cx="{bx:.1f}" cy="{by:.1f}" r="12" />')
            parts.append(f'<text class="sb-transit-txt" x="{bx:.1f}" y="{by + 1:.1f}">{not_in_place}</text>')

    if source_track is not None:
        parts.append(_schematic_endpoint_svg(layout, source_track, label="起", css_class="schematic-endpoint"))
    if target_track is not None:
        parts.append(_schematic_endpoint_svg(layout, target_track, label="终", css_class="schematic-endpoint-target"))

    if hook is not None and hook.vehicle_nos:
        if animate and motion_path:
            marker_label = escape(str(len(hook.vehicle_nos)))
            parts.append('<g class="moving-block-marker">')
            parts.append('<circle class="moving-block-marker" r="18" cx="0" cy="0" />')
            parts.append(f'<text class="moving-block-label" x="0" y="1">{marker_label}</text>')
            parts.append(
                '<animateMotion dur="2.4s" repeatCount="indefinite" rotate="auto">'
                '<mpath href="#route-motion-path" />'
                '</animateMotion>'
            )
            parts.append("</g>")
        elif transition_frame is not None:
            point = point_at_progress(route, transition_frame.progress) if motion_path else None
            if point is None:
                point = route.points[0] if route.points else LayoutPoint(x=0.0, y=0.0)
            parts.append(f'<circle class="moving-block-marker" cx="{point.x:.1f}" cy="{point.y:.1f}" r="18" />')
            parts.append(
                f'<text class="moving-block-label" x="{point.x:.1f}" y="{point.y + 1:.1f}">{len(hook.vehicle_nos)}</text>'
            )

    if track_map is not None:
        loco_track = next((track_code for track_code, node in track_nodes.items() if node.has_loco), None)
    else:
        loco_track = None
    if loco_track is not None and loco_track in layout.track_geometries:
        loco_center = layout.track_geometries[loco_track].center
        parts.append(
            f'<rect class="loco-marker" x="{loco_center.x - 12:.1f}" y="{loco_center.y - 48:.1f}" width="24" height="24" rx="6" />'
        )

    parts.append("</svg>")
    return "".join(parts)


def _track_polyline_to_svg(points) -> str:
    if not points:
        return ""
    first = points[0]
    segments = [f"M {first.x:.1f} {first.y:.1f}"]
    for point in points[1:]:
        segments.append(f"L {point.x:.1f} {point.y:.1f}")
    return " ".join(segments)


def _track_badge_width(track_code: str) -> float:
    return max(64.0, 18.0 * len(track_code) + 26.0)


def _background_rect_svg(rect, css_class: str) -> str:
    center_x = rect.x + rect.width / 2.0
    center_y = rect.y + rect.height / 2.0
    rotation = rect.rotation_deg
    transform = ""
    if abs(rotation) > 1e-6:
        transform = f' transform="rotate({rotation:.1f} {center_x:.1f} {center_y:.1f})"'
    return (
        f'<rect class="{css_class}" x="{rect.x:.1f}" y="{rect.y:.1f}" '
        f'width="{rect.width:.1f}" height="{rect.height:.1f}" rx="10"{transform} />'
    )


def _build_background_anchor_route(layout, track_codes: list[str]):
    points: list[LayoutPoint] = []
    for track_code in track_codes:
        geometry = layout.track_geometries.get(track_code)
        if geometry is None:
            continue
        anchor = geometry.background_anchor or geometry.center
        if not points or points[-1] != anchor:
            points.append(anchor)
    if not points:
        return build_route_polyline(layout, track_codes)
    total_length_px = 0.0
    cumulative_lengths = [0.0]
    for start, end in zip(points, points[1:], strict=False):
        total_length_px += ((end.x - start.x) ** 2 + (end.y - start.y) ** 2) ** 0.5
        cumulative_lengths.append(total_length_px)
    from fzed_shunting.demo.layout import RoutePolyline

    return RoutePolyline(
        track_codes=list(track_codes),
        points=points,
        total_length_px=total_length_px,
        cumulative_lengths=cumulative_lengths,
    )


def _should_render_track_overlay(track_code, node, active_tracks: set[str], changed_tracks: set[str]) -> bool:
    if track_code in active_tracks or track_code in changed_tracks:
        return True
    if node is None:
        return False
    return node.has_loco


def _get_topology_layout():
    global _TOPOLOGY_LAYOUT
    if _TOPOLOGY_LAYOUT is None:
        _TOPOLOGY_LAYOUT = load_topology_layout(MASTER_DIR, _get_master_data())
    return _TOPOLOGY_LAYOUT


def _get_schematic_layout():
    global _SCHEMATIC_LAYOUT
    if _SCHEMATIC_LAYOUT is None:
        _SCHEMATIC_LAYOUT = load_schematic_layout(MASTER_DIR)
    return _SCHEMATIC_LAYOUT


def _schematic_endpoint_svg(layout, track_code: str, *, label: str, css_class: str) -> str:
    geometry = layout.track_geometries.get(track_code)
    if geometry is None:
        return ""
    center_x = geometry.label_anchor.x
    center_y = geometry.label_anchor.y - 26.0
    return (
        f'<g><circle class="{css_class}" cx="{center_x:.1f}" cy="{center_y:.1f}" r="13" />'
        f'<text class="schematic-endpoint-text" x="{center_x:.1f}" y="{center_y + 1:.1f}">{escape(label)}</text></g>'
    )


def _hook_title(hook) -> str:
    if hook.action_type == "ATTACH":
        return f"挂车 ← {hook.source_track}"
    if hook.action_type == "DETACH":
        return f"摘车 → {hook.target_track}"
    return f"{hook.source_track} → {hook.target_track}"


def _build_hook_sidebar_rows(step) -> list[dict[str, str]]:
    if step.hook is None:
        return [
            {"label": "状态", "value": "初始状态"},
            {"label": "机车位置", "value": step.loco_track_name},
            {"label": "变化股道", "value": "无"},
        ]

    route_length = f"{step.hook.route_length_m:.1f}m" if step.hook.route_length_m is not None else "未知"
    action_type = step.hook.action_type
    track_rows: list[dict[str, str]]
    if action_type == "ATTACH":
        track_rows = [{"label": "挂车股道", "value": step.hook.source_track}]
    elif action_type == "DETACH":
        track_rows = [{"label": "摘车股道", "value": step.hook.target_track}]
    else:
        track_rows = [
            {"label": "起点", "value": step.hook.source_track},
            {"label": "终点", "value": step.hook.target_track},
        ]
    return [
        {"label": "当前钩", "value": f"第 {step.hook.hook_no} 钩"},
        {"label": "动作", "value": action_type},
        *track_rows,
        {"label": "车辆数", "value": str(step.hook.vehicle_count)},
        {"label": "机车位置", "value": step.loco_track_name},
        {"label": "路径长度", "value": route_length},
        {"label": "变化股道", "value": ", ".join(step.changed_tracks) if step.changed_tracks else "无"},
    ]


def _build_step_state_rows(
    track_map,
    vehicle_display_metadata: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    vehicle_meta = vehicle_display_metadata or {}
    rows: list[dict[str, str]] = []
    for track_code, node in track_map.track_nodes.items():
        if not (node.is_in_active_path or node.is_changed or node.is_occupied or node.has_loco):
            continue
        state_parts: list[str] = []
        if node.has_loco:
            state_parts.append("机车")
        if node.is_in_active_path:
            state_parts.append("当前路径")
        if node.is_changed:
            state_parts.append("本步变化")
        state_parts.append(f"占用 {len(node.vehicle_nos)}" if node.is_occupied else "空")
        rows.append(
            {
                "trackCode": track_code,
                "state": " / ".join(state_parts),
                "vehicles": (
                    _format_hook_vehicle_text(node.vehicle_nos, vehicle_meta)
                    if node.vehicle_nos
                    else "无车辆"
                ),
            }
        )
    rows.sort(key=lambda item: item["trackCode"])
    return rows


def _build_distance_breakdown_rows(hook) -> list[dict[str, object]]:
    if hook is None:
        return []
    master = _get_master_data()
    rows: list[dict[str, object]] = []
    running_total = 0.0
    for index, track_code in enumerate(hook.path_tracks, start=1):
        track = master.tracks.get(track_code)
        if track is None:
            continue
        running_total += track.effective_length_m
        rows.append(
            {
                "order": index,
                "trackCode": track_code,
                "trackName": track.name,
                "effectiveLengthM": track.effective_length_m,
                "cumulativeEffectiveLengthM": round(running_total, 1),
            }
        )
    return rows


def _build_distance_catalog_rows() -> list[dict[str, object]]:
    master = _get_master_data()
    routes = load_segmented_physical_routes(MASTER_DIR, master)
    rows: list[dict[str, object]] = []
    for route in routes.values():
        rows.append(
            {
                "displayName": route.display_name,
                "branchCode": route.branch_code,
                "endpointSpan": f"{route.left_node or '?'} -> {route.right_node or '?'}",
                "segments": " / ".join(segment.track_code for segment in route.segments),
                "totalPhysicalDistanceM": route.aggregate_physical_distance_m,
            }
        )
    return rows


def _render_hook_sidebar(step, vehicle_target_tracks: dict | None = None) -> None:
    route_tracks = step.hook.path_tracks if step.hook is not None else []
    track_nodes = step.track_map.track_nodes if step.track_map else {}
    vtt = vehicle_target_tracks or {}
    cards = []
    for row in _build_hook_sidebar_rows(step):
        cards.append(
            f"""
            <div class="hook-sidebar-card">
              <div class="hook-sidebar-label">{escape(row['label'])}</div>
              <div class="hook-sidebar-value">{escape(row['value'])}</div>
            </div>
            """
        )

    chip_parts = []
    for track_code in route_tracks:
        node = track_nodes.get(track_code)
        vehicles = node.vehicle_nos if node and node.vehicle_nos else []
        in_place = sum(1 for v in vehicles if track_code in vtt.get(v, []))
        not_in_place = len(vehicles) - in_place
        if vehicles:
            count_html = ""
            if in_place > 0:
                count_html += f"<span class='rc-parked'>{in_place}</span>"
            if not_in_place > 0:
                count_html += f"<span class='rc-transit'>{not_in_place}</span>"
            chip_parts.append(
                f"<span class='route-chip route-chip-occupied'>"
                f"{escape(track_code)}{count_html}"
                f"</span>"
            )
        else:
            chip_parts.append(f"<span class='route-chip'>{escape(track_code)}</span>")
    chips = "".join(chip_parts) or "<span class='route-chip route-chip-muted'>初始状态</span>"

    st.markdown(
        """
        <style>
        .hook-sidebar-grid {
          display: grid;
          grid-template-columns: 1fr;
          gap: 10px;
          margin-bottom: 12px;
        }
        .hook-sidebar-card {
          padding: 12px 14px;
          border-radius: 14px;
          background: linear-gradient(180deg, #fffaf1 0%, #f4ecde 100%);
          border: 1px solid #e3d7c0;
        }
        .hook-sidebar-label {
          font-size: 12px;
          color: #7c6f59;
          margin-bottom: 4px;
        }
        .hook-sidebar-value {
          font-size: 16px;
          font-weight: 700;
          color: #2f2a22;
        }
        .route-chip-row {
          display: flex;
          flex-wrap: wrap;
          gap: 8px;
          margin-top: 8px;
        }
        .route-chip {
          padding: 6px 10px;
          border-radius: 999px;
          background: #dcf2ee;
          color: #0f4f4a;
          font-size: 13px;
          font-weight: 600;
        }
        .route-chip-occupied {
          background: #fff0d6;
          color: #7c3f00;
          border: 1px solid #e8a82a;
        }
        .route-chip-count {
          display: inline-block;
          background: #d97706;
          color: #ffffff;
          border-radius: 999px;
          font-size: 11px;
          padding: 1px 6px;
          margin-left: 5px;
        }
        .rc-parked {
          display: inline-block;
          background: #0f766e;
          color: #ffffff;
          border-radius: 999px;
          font-size: 11px;
          padding: 1px 6px;
          margin-left: 5px;
        }
        .rc-transit {
          display: inline-block;
          background: #d97706;
          color: #ffffff;
          border-radius: 999px;
          font-size: 11px;
          padding: 1px 6px;
          margin-left: 3px;
        }
        .route-chip-muted {
          background: #f1ede5;
          color: #7c6f59;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(f"<div class='hook-sidebar-grid'>{''.join(cards)}</div>", unsafe_allow_html=True)
    st.markdown("**路径条**")
    st.markdown(f"<div class='route-chip-row'>{chips}</div>", unsafe_allow_html=True)


def _build_vehicle_roster_rows(payload: dict) -> list[dict[str, object]]:
    """Build per-vehicle overview rows from raw scenario payload.

    Includes current track, destination (目的地), and attributes so operators
    can see the full vehicle distribution and their goals at a glance.
    """

    rows: list[dict[str, object]] = []
    source = payload.get("initialVehicleInfo") or payload.get("vehicleInfo") or []
    # For workflow stages, targetTrack is not on initialVehicleInfo; fall back
    # to vehicleInfo for per-stage goals when both present.
    target_map: dict[str, str] = {}
    for item in payload.get("vehicleInfo", []) or []:
        vehicle_no = str(item.get("vehicleNo", ""))
        target = item.get("targetTrack")
        if vehicle_no and target:
            target_map[vehicle_no] = str(target)
    for item in source:
        vehicle_no = str(item.get("vehicleNo", ""))
        rows.append(
            {
                "vehicleNo": vehicle_no,
                "currentTrack": str(item.get("trackName", "")),
                "targetTrack": target_map.get(vehicle_no) or str(item.get("targetTrack", "") or ""),
                "vehicleModel": str(item.get("vehicleModel", "")),
                "vehicleLength": item.get("vehicleLength", ""),
                "repairProcess": str(item.get("repairProcess", "")),
                "attributes": str(item.get("vehicleAttributes", "") or ""),
            }
        )
    rows.sort(key=lambda r: (r["currentTrack"], r["vehicleNo"]))
    return rows


def _build_vehicle_display_metadata(payload: dict) -> dict[str, dict[str, str]]:
    metadata: dict[str, dict[str, str]] = {}
    sources = []
    initial_vehicle_info = payload.get("initialVehicleInfo")
    if isinstance(initial_vehicle_info, list):
        sources.append(initial_vehicle_info)
    vehicle_info = payload.get("vehicleInfo")
    if isinstance(vehicle_info, list):
        sources.append(vehicle_info)
    for source in sources:
        for item in source:
            vehicle_no = str(item.get("vehicleNo", "")).strip()
            if not vehicle_no:
                continue
            attributes_value = str(item.get("vehicleAttributes", "") or "").strip()
            metadata[vehicle_no] = {
                "requirement": _format_vehicle_requirement_text(item),
                "attributes": attributes_value or "无",
                "length": _format_vehicle_length_text(item.get("vehicleLength")),
            }
    return metadata


def _format_vehicle_requirement_text(vehicle_info: dict) -> str:
    target_track = str(vehicle_info.get("targetTrack", "") or "").strip()
    spotting_value = str(vehicle_info.get("isSpotting", "") or "").strip()
    target_spot_code = str(vehicle_info.get("targetSpotCode", "") or "").strip()
    if target_spot_code:
        return target_spot_code
    if spotting_value.isdigit() or spotting_value == "迎检":
        return spotting_value
    if spotting_value == "是":
        return f"{target_track}作业区" if target_track else "需要对位"
    if target_track:
        return target_track
    return "无"


def _format_vehicle_length_text(length_value) -> str:
    try:
        return f"{float(length_value):.1f}m"
    except (TypeError, ValueError):
        return "未知"


def _format_vehicle_display_text(
    vehicle_no: str,
    vehicle_display_metadata: dict[str, dict[str, str]] | None = None,
) -> str:
    vehicle_meta = (vehicle_display_metadata or {}).get(vehicle_no)
    if not vehicle_meta:
        return vehicle_no
    return (
        f"{vehicle_no}(对位={vehicle_meta['requirement']}，"
        f"属性={vehicle_meta['attributes']}，"
        f"长度={vehicle_meta['length']})"
    )


def _format_hook_vehicle_text(
    vehicle_nos: list[str],
    vehicle_display_metadata: dict[str, dict[str, str]] | None = None,
) -> str:
    return " ".join(
        _format_vehicle_display_text(vehicle_no, vehicle_display_metadata)
        for vehicle_no in vehicle_nos
    )


def _format_pre_hook_loco_carry_text(
    view,
    step_index: int,
    vehicle_display_metadata: dict[str, dict[str, str]] | None = None,
) -> str:
    previous_step_index = step_index - 1
    if previous_step_index < 0 or previous_step_index >= len(view.steps):
        carry_vehicle_nos: list[str] = []
    else:
        carry_vehicle_nos = list(
            getattr(view.steps[previous_step_index], "loco_carry_vehicle_nos", [])
        )
    if not carry_vehicle_nos:
        return "本钩前调车机后挂: 无"
    return (
        "本钩前调车机后挂: "
        f"{_format_hook_vehicle_text(carry_vehicle_nos, vehicle_display_metadata)}"
    )


def _render_vehicle_detail_panel(step, view, vehicle_display_metadata: dict[str, dict[str, str]] | None = None) -> None:
    vehicle_meta = vehicle_display_metadata or {}
    if step.hook is not None and step.hook.vehicle_nos:
        action_type = step.hook.action_type
        st.dataframe(
            [
                {
                    "vehicleNo": _format_vehicle_display_text(vehicle_no, vehicle_meta),
                    **({} if action_type == "DETACH" else {"sourceTrack": step.hook.source_track}),
                    **({} if action_type == "ATTACH" else {"targetTrack": step.hook.target_track}),
                }
                for vehicle_no in step.hook.vehicle_nos
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("当前步骤无车辆移动。")
    if step.weighed_vehicle_nos:
        st.caption(f"本步已称重车辆: {_format_hook_vehicle_text(step.weighed_vehicle_nos, vehicle_meta)}")
    if step.spot_assignments:
        st.markdown("**当前台位分配**")
        st.dataframe(
            [
                {
                    "vehicleNo": _format_vehicle_display_text(vehicle_no, vehicle_meta),
                    "spotCode": spot_code,
                }
                for vehicle_no, spot_code in step.spot_assignments.items()
            ],
            use_container_width=True,
            hide_index=True,
        )
    if step.work_position_assignments:
        st.markdown("**当前作业线序位**")
        st.dataframe(
            [
                {
                    "vehicleNo": _format_vehicle_display_text(vehicle_no, vehicle_meta),
                    "track": item.get("track"),
                    "northRank": item.get("northRank"),
                    "southRank": item.get("southRank"),
                    "rule": item.get("rule"),
                    "targetRank": item.get("targetRank"),
                    "satisfied": item.get("satisfied"),
                }
                for vehicle_no, item in step.work_position_assignments.items()
            ],
            use_container_width=True,
            hide_index=True,
        )
    elif view.final_spot_assignments:
        st.markdown("**最终台位分配**")
        st.dataframe(
            [
                {
                    "vehicleNo": _format_vehicle_display_text(vehicle_no, vehicle_meta),
                    "spotCode": spot_code,
                }
                for vehicle_no, spot_code in view.final_spot_assignments.items()
            ],
            use_container_width=True,
            hide_index=True,
        )
    elif view.final_work_position_assignments:
        st.markdown("**最终作业线序位**")
        st.dataframe(
            [
                {
                    "vehicleNo": _format_vehicle_display_text(vehicle_no, vehicle_meta),
                    "track": item.get("track"),
                    "northRank": item.get("northRank"),
                    "southRank": item.get("southRank"),
                    "rule": item.get("rule"),
                    "targetRank": item.get("targetRank"),
                    "satisfied": item.get("satisfied"),
                }
                for vehicle_no, item in view.final_work_position_assignments.items()
            ],
            use_container_width=True,
            hide_index=True,
        )


def _render_verifier_panel(step, view) -> None:
    if step.verifier_errors:
        st.error("当前步骤校验未通过")
        st.write(step.verifier_errors)
    elif view.verifier_errors:
        st.warning("整体计划存在校验问题，但当前步骤未命中错误。")
        st.write(view.verifier_errors[:2])
    else:
        st.success("当前步骤与整体计划校验通过")


@lru_cache(maxsize=1)
def _topology_background_data_uri(path: str, crop_box: tuple[int, int, int, int] | None) -> str:
    image = Image.open(path).convert("RGB")
    if crop_box is not None:
        image = image.crop(crop_box)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _get_topology_background_data_uri(layout) -> str | None:
    background = layout.background_image
    if background is None:
        return None
    image_path = (MASTER_DIR / background.path).resolve()
    return _topology_background_data_uri(str(image_path), background.crop_box)


def _render_track_grid(
    track_sequences: dict[str, list[str]],
    highlighted_tracks: set[str],
):
    active_tracks = sorted(
        track for track, seq in track_sequences.items() if seq or track in highlighted_tracks
    )
    if not active_tracks:
        st.info("当前无车辆占用。")
        return

    st.markdown("**当前股道状态**")
    columns = st.columns(3)
    for index, track in enumerate(active_tracks):
        sequence = track_sequences.get(track, [])
        with columns[index % 3]:
            label = f"{track} *" if track in highlighted_tracks else track
            st.markdown(f"**{label}**")
            if sequence:
                st.code(
                    "\n".join(f"{position}. {vehicle_no}" for position, vehicle_no in enumerate(sequence, start=1)),
                    language=None,
                )
            else:
                st.caption("空")


def _render_track_map(track_map):
    ordered_tracks = list(track_map.active_path_tracks)
    ordered_tracks.extend(
        track_code
        for track_code, node in track_map.track_nodes.items()
        if track_code not in ordered_tracks and node.is_occupied
    )
    ordered_tracks.extend(
        track_code
        for track_code in track_map.changed_tracks
        if track_code not in ordered_tracks
    )
    if not ordered_tracks:
        ordered_tracks = list(track_map.track_nodes.keys())

    st.markdown(
        """
        <style>
        .track-map-grid {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
          gap: 10px;
          margin: 8px 0 14px 0;
        }
        .track-map-card {
          border-radius: 12px;
          padding: 10px 12px;
          border: 1px solid #d8d1c2;
          background: linear-gradient(180deg, #faf7f2 0%, #f0ebe2 100%);
        }
        .track-map-card.active {
          border-color: #1d6f6d;
          background: linear-gradient(180deg, #eefaf8 0%, #dcf0ec 100%);
        }
        .track-map-card.changed {
          box-shadow: inset 0 0 0 1px #d8772a;
        }
        .track-map-card.loco {
          background: linear-gradient(180deg, #fff5e8 0%, #fde7cf 100%);
        }
        .track-map-title {
          font-weight: 700;
          margin-bottom: 3px;
        }
        .track-map-meta {
          font-size: 12px;
          color: #5d5a54;
          margin-bottom: 4px;
        }
        .track-map-body {
          font-size: 13px;
          color: #23211d;
          word-break: break-word;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.caption(_track_map_legend_markdown())

    cards: list[str] = []
    for track_code in ordered_tracks:
        node = track_map.track_nodes[track_code]
        classes = ["track-map-card"]
        if node.is_in_active_path:
            classes.append("active")
        if node.is_changed:
            classes.append("changed")
        if node.has_loco:
            classes.append("loco")
        status_parts: list[str] = []
        if node.has_loco:
            status_parts.append("机车")
        status_parts.append(f"占用 {len(node.vehicle_nos)}" if node.is_occupied else "空")
        body = " ".join(node.vehicle_nos) if node.vehicle_nos else "无车辆"
        cards.append(
            f"""
            <div class="{' '.join(classes)}">
              <div class="track-map-title">{track_code}</div>
              <div class="track-map-meta">{' / '.join(status_parts)}</div>
              <div class="track-map-body">{body}</div>
            </div>
            """
        )

    st.markdown(
        f"<div class='track-map-grid'>{''.join(cards)}</div>",
        unsafe_allow_html=True,
    )


def _workflow_progress_value(*, stage_index: int, stage_count: int) -> float:
    if stage_count <= 1:
        return 1.0
    return stage_index / (stage_count - 1)


def _build_workflow_stage_rows(workflow) -> list[dict]:
    rows: list[dict] = []
    for index, stage in enumerate(workflow.stages, start=1):
        view = stage.view
        rows.append(
            {
                "stageIndex": index,
                "stageName": stage.name,
                "hookCount": view.summary.hook_count if view else 0,
                "finalTracks": ", ".join(view.summary.final_tracks) if view else "",
                "isValid": view.summary.is_valid if view else False,
            }
        )
    return rows


def _build_workflow_transition_rows(workflow) -> list[dict]:
    rows: list[dict] = []
    previous_tracks: dict[str, str] = {}
    previous_weighed: set[str] = set()
    previous_spots: dict[str, str] = {}

    for index, stage in enumerate(workflow.stages, start=1):
        view = stage.view
        if view is None or not view.steps:
            rows.append(
                {
                    "stageIndex": index,
                    "stageName": stage.name,
                    "locoTransition": "无",
                    "movedVehicles": "无",
                    "newWeighedVehicles": "无",
                    "spotChanges": "无",
                }
            )
            continue
        first_step = view.steps[0]
        final_step = view.steps[-1]
        current_tracks = {
            vehicle_no: track_name
            for track_name, seq in final_step.track_sequences.items()
            for vehicle_no in seq
        }
        current_weighed = set(final_step.weighed_vehicle_nos)
        current_spots = dict(final_step.spot_assignments)

        moved_parts: list[str] = []
        for vehicle_no in sorted(current_tracks):
            previous_track = previous_tracks.get(vehicle_no)
            current_track = current_tracks[vehicle_no]
            if previous_track is None:
                previous_track = _find_vehicle_track(first_step.track_sequences, vehicle_no) or current_track
            if previous_track != current_track:
                moved_parts.append(f"{vehicle_no}({previous_track}->{current_track})")

        spot_parts: list[str] = []
        for vehicle_no in sorted(set(previous_spots) | set(current_spots)):
            previous_spot = previous_spots.get(vehicle_no, "无")
            current_spot = current_spots.get(vehicle_no, "无")
            if previous_spot != current_spot:
                spot_parts.append(f"{vehicle_no}({previous_spot}->{current_spot})")

        rows.append(
            {
                "stageIndex": index,
                "stageName": stage.name,
                "locoTransition": f"{first_step.loco_track_name} -> {final_step.loco_track_name}",
                "movedVehicles": "；".join(moved_parts) if moved_parts else "无",
                "newWeighedVehicles": (
                    " ".join(sorted(current_weighed - previous_weighed))
                    if current_weighed - previous_weighed
                    else "无"
                ),
                "spotChanges": "；".join(spot_parts) if spot_parts else "无",
            }
        )

        previous_tracks = current_tracks
        previous_weighed = current_weighed
        previous_spots = current_spots
    return rows


def _find_vehicle_track(track_sequences: dict[str, list[str]], vehicle_no: str) -> str | None:
    for track_name, seq in track_sequences.items():
        if vehicle_no in seq:
            return track_name
    return None


def _track_map_legend_markdown() -> str:
    return "Active Path = 绿色, Changed Track = 橙色描边, Loco Track = 橙色底色"


if __name__ == "__main__":
    main()
