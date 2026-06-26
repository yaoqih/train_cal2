from __future__ import annotations

import base64
from functools import lru_cache
from io import BytesIO
import json
from pathlib import Path
from html import escape

import streamlit as st
from PIL import Image

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


MASTER_DIR = Path(__file__).resolve().parent / "data" / "master"
DEFAULT_EVAL_ARTIFACT = (
    Path(__file__).resolve().parent
    / "artifacts"
    / "l7_phase1234_truth_phase3_tail_run_preflight_20260614.json"
)
_TOPOLOGY_LAYOUT = None
_SCHEMATIC_LAYOUT = None
_MASTER_DATA = None


def _get_master_data():
    global _MASTER_DATA
    if _MASTER_DATA is None:
        _MASTER_DATA = load_master_data(MASTER_DIR)
    return _MASTER_DATA


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
    st.caption("按场景文件回放 block 级求解结果，并显示 verifier 结论。")

    demo_tab, eval_tab = st.tabs(["案例回放", "评估统计"])
    with eval_tab:
        _render_evaluation_dashboard()
    with demo_tab:
        _render_single_scenario_page()


def _render_single_scenario_page() -> None:
    st.subheader("案例回放")

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
