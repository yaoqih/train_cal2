from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
import hashlib
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
P10_SCHEMATIC_LAYOUT_PATH = ROOT_DIR / "data" / "map" / "schematic_layout.json"
VNEXT_RUNTIME_SCRIPT_PATH = SCRIPTS_DIR / "generate_vnext_runtime_trace.py"
VNEXT_SOLVER_DIR = SCRIPTS_DIR / "solver_vnext"
APP_VNEXT_OUTPUT_DIR = ROOT_DIR / "artifacts" / "app_vnext_runtime"
DEFAULT_EVAL_ARTIFACT = (
    Path(__file__).resolve().parent
    / "artifacts"
    / "l7_phase1234_truth_phase3_tail_run_preflight_20260614.json"
)
DEFAULT_STAGE1_SIMPLE_DIR = ROOT_DIR / "artifacts" / "stage1_simple_goal_verify"
DEFAULT_STAGE2_SIMPLE_DIR = ROOT_DIR / "artifacts" / "stage2_simple_final"
_VNEXT_RUNTIME_CACHE = None
P10_BUSINESS_HOOK_ACTIONS = {"Get", "Put"}
SOLVER_VNEXT = "vNext 求解器"


@dataclass(frozen=True)
class VNextPageSummary:
    case_id: str
    solve_strategy: str
    status: str
    input_schema_passed: bool
    vehicle_count: int
    initial_unsatisfied_vehicle_count: int
    final_unsatisfied_vehicle_count: int
    business_get_put_hook_count: int
    internal_move_batch_count: int
    interface_operation_count: int
    get_operation_count: int
    put_operation_count: int
    weigh_operation_count: int
    remote_interaction_cross_count: int
    remote_business_transition_count: int
    remote_interaction_batch_count: int
    remote_interaction_session_count: int
    generated_hook_count: int
    generated_operation_count: int
    accepted_candidate_count: int
    rejected_candidate_count: int
    blocked_candidate_count: int
    hard_physical_violation_accepted_count: int
    unknown_route_count: int
    depot_slot_failure_count: int
    state_loop_count: int
    final_capacity_warning_count: int
    final_capacity_warning_reasons: str
    closed_door_replay_violation_count: int
    closed_door_replay_violation_reasons: str
    blocked_reason: str
    response_path: str


@dataclass(frozen=True)
class Stage1OperationRow:
    hook_index: int
    operation_index: int
    action: str
    line: str
    move_cars: str
    train_cars: str
    passby_path: str


def _p10_file_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return 0


def _vnext_runtime_fingerprint() -> tuple[tuple[str, int], ...]:
    paths = [
        VNEXT_RUNTIME_SCRIPT_PATH,
        *sorted(VNEXT_SOLVER_DIR.glob("*.py")),
    ]
    return tuple(
        (str(path.relative_to(ROOT_DIR)), _p10_file_mtime_ns(path))
        for path in paths
    )


def _vnext_runtime_version_text() -> str:
    digest = hashlib.sha1(repr(_vnext_runtime_fingerprint()).encode("utf-8")).hexdigest()[:8]
    return f"vNext solver {digest}"


def _vnext_runtime_module():
    global _VNEXT_RUNTIME_CACHE
    fingerprint = _vnext_runtime_fingerprint()
    if _VNEXT_RUNTIME_CACHE is not None and _VNEXT_RUNTIME_CACHE[0] == fingerprint:
        return _VNEXT_RUNTIME_CACHE[1]

    scripts_path = str(SCRIPTS_DIR)
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)

    for name in sorted(
        [name for name in sys.modules if name == "solver_vnext" or name.startswith("solver_vnext.")],
        key=lambda item: item.count("."),
        reverse=True,
    ):
        del sys.modules[name]

    vnext_engine = importlib.import_module("solver_vnext.engine")
    _VNEXT_RUNTIME_CACHE = (fingerprint, vnext_engine)
    return vnext_engine


def _vnext_physical_module():
    scripts_path = str(SCRIPTS_DIR)
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)
    return importlib.import_module("solver_vnext.physical")


def _payload_cache_key(payload) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def main():
    st.set_page_config(page_title="福州东调车 vNext Demo", layout="wide")
    st.title("福州东调车 vNext Demo")
    st.caption("输入取送车计划，运行 vNext 求解演示，并查看评估统计。")

    p10_tab, stage1_tab, stage2_tab, eval_tab = st.tabs(["vNext 求解演示", "第一阶段可视化", "第二阶段可视化", "评估统计"])
    with p10_tab:
        _render_p10_runtime_page()
    with stage1_tab:
        _render_stage1_simple_dashboard()
    with stage2_tab:
        _render_stage2_simple_dashboard()
    with eval_tab:
        _render_evaluation_dashboard()


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


def _render_p10_runtime_page() -> None:
    st.subheader("求解演示")
    st.caption("读取接口形态 JSON，调用 vNext 求解器输出 Operations 和 GeneratedEndStatus。")
    solver_kind = SOLVER_VNEXT

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

    version_text = _vnext_runtime_version_text()
    st.caption(
        f"{version_text}；业务对人工比较应看挂/摘勾数，"
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
    run_requested = st.button(f"运行 {solver_kind}", type="primary", key="p10-run")
    if not run_requested:
        last_result = st.session_state.get("p10_last_result")
        if last_result:
            stale_reason = _p10_stale_result_reason(last_result, source_name, solver_kind)
            st.divider()
            if stale_reason:
                st.warning(stale_reason)
                if st.button("清空过期结果", key="p10-clear-stale-result"):
                    st.session_state.pop("p10_last_result", None)
                    st.rerun()
                return
            st.caption(f"显示上一次 {solver_kind} 结果；修改输入后需要重新点击运行按钮才会更新。")
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
        with st.spinner(f"{solver_kind} 求解中..."):
            summary, candidate_rows, operation_rows, rejection_reasons, response = _run_vnext_case(
                truth_path=runtime_input_path,
                payload=runtime_payload,
                max_hooks=int(max_hooks),
            )
            runtime_fingerprint = _vnext_runtime_fingerprint()
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
        "runtime_fingerprint": runtime_fingerprint,
        "source_name": source_name,
        "solver_kind": solver_kind,
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


def _p10_stale_result_reason(last_result: dict, source_name: str, solver_kind: str = SOLVER_VNEXT) -> str:
    expected_fingerprint = _vnext_runtime_fingerprint()
    last_solver_kind = last_result.get("solver_kind") or SOLVER_VNEXT
    if last_solver_kind != solver_kind:
        return f"上一次结果由 {last_solver_kind} 生成，当前选择的是 {solver_kind}；请重新点击运行按钮。"
    if last_result.get("runtime_fingerprint") != expected_fingerprint:
        return f"上一次 {solver_kind} 结果由旧代码生成，已不再展示；请重新点击运行按钮。"
    last_source = str(last_result.get("source_name") or "")
    if last_source and source_name and last_source != source_name:
        return f"上一次 {solver_kind} 结果属于另一个计划，已不再展示；请重新点击运行按钮。"
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
    input_dir = APP_VNEXT_OUTPUT_DIR / "inputs"
    input_dir.mkdir(parents=True, exist_ok=True)
    path = input_dir / f"vnext_plan_{case_id}_{digest}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _run_vnext_case(
    *,
    truth_path: Path,
    payload: dict,
    max_hooks: int,
):
    vnext_engine = _vnext_runtime_module()
    solver = vnext_engine.VNextSolver(max_hooks=max_hooks)
    case_result, step_traces, operation_rows, *_diagnostic_rows = solver.solve_case(truth_path, APP_VNEXT_OUTPUT_DIR)
    summary = _vnext_summary_for_p10_page(
        case_result=case_result,
        operation_rows=operation_rows,
        step_traces=step_traces,
        payload=payload,
    )
    response = _vnext_load_response_or_build(summary, operation_rows, payload)
    rejection_reasons = Counter(
        trace.gate_reason or "unknown"
        for trace in step_traces
        if not trace.selected
    )
    return summary, step_traces, operation_rows, rejection_reasons, response


def _vnext_summary_for_p10_page(*, case_result, operation_rows, step_traces, payload: dict):
    get_operation_count = sum(1 for row in operation_rows if row.action == "Get")
    put_operation_count = sum(1 for row in operation_rows if row.action == "Put")
    weigh_operation_count = sum(1 for row in operation_rows if row.action == "Weigh")
    remote_cross_count, remote_batch_count, remote_session_count = _vnext_remote_interaction_metrics(operation_rows)
    final_cars = _vnext_final_cars_from_response_or_payload(
        case_id=case_result.case_id,
        payload=payload,
        use_response=bool(operation_rows),
    )
    final_capacity_warnings = (
        _vnext_final_line_capacity_warning_reasons(final_cars)
        if case_result.status == "completed"
        else []
    )
    response_path = str(APP_VNEXT_OUTPUT_DIR / "responses" / f"{case_result.case_id}.json")
    return VNextPageSummary(
        case_id=case_result.case_id,
        solve_strategy="vnext_contract_resource_delta_episode",
        status=case_result.status,
        input_schema_passed=True,
        vehicle_count=len(payload.get("StartStatus") or []),
        initial_unsatisfied_vehicle_count=case_result.initial_unsatisfied,
        final_unsatisfied_vehicle_count=case_result.final_unsatisfied,
        business_get_put_hook_count=get_operation_count + put_operation_count,
        internal_move_batch_count=len({row.hook_index for row in operation_rows}),
        interface_operation_count=case_result.operation_count,
        get_operation_count=get_operation_count,
        put_operation_count=put_operation_count,
        weigh_operation_count=weigh_operation_count,
        remote_interaction_cross_count=remote_cross_count,
        remote_business_transition_count=case_result.remote_business_transition_count,
        remote_interaction_batch_count=remote_batch_count,
        remote_interaction_session_count=remote_session_count,
        generated_hook_count=len({row.hook_index for row in operation_rows}),
        generated_operation_count=case_result.operation_count,
        accepted_candidate_count=case_result.step_count,
        rejected_candidate_count=sum(1 for trace in step_traces if not trace.selected),
        blocked_candidate_count=1 if case_result.status == "blocked" else 0,
        hard_physical_violation_accepted_count=case_result.hard_physical_violation_accepted_count,
        unknown_route_count=sum(1 for trace in step_traces if "route_missing" in (trace.physical_reasons or "")),
        depot_slot_failure_count=0,
        state_loop_count=sum(1 for trace in step_traces if trace.gate_reason == "loop_reject"),
        final_capacity_warning_count=len(final_capacity_warnings),
        final_capacity_warning_reasons="|".join(final_capacity_warnings),
        closed_door_replay_violation_count=_vnext_closed_door_violation_count(case_result.blocked_reason),
        closed_door_replay_violation_reasons=_vnext_closed_door_violation_reasons(case_result.blocked_reason),
        blocked_reason=case_result.blocked_reason,
        response_path=response_path,
    )


def _vnext_remote_interaction_metrics(operation_rows) -> tuple[int, int, int]:
    physical = _vnext_physical_module()
    business_rows = sorted(
        (row for row in operation_rows if row.action in P10_BUSINESS_HOOK_ACTIONS),
        key=lambda row: (row.hook_index, row.operation_index),
    )
    remote_flags = [row.line in physical.REMOTE_INTERACTION_LINES for row in business_rows]
    cross_count = sum(
        left != right
        for left, right in zip(remote_flags, remote_flags[1:])
    )
    remote_batch_count = len(
        {
            row.hook_index
            for row in business_rows
            if row.line in physical.REMOTE_INTERACTION_LINES
        }
    )
    remote_session_count = sum(
        is_remote and (index == 0 or not remote_flags[index - 1])
        for index, is_remote in enumerate(remote_flags)
    )
    return cross_count, remote_batch_count, remote_session_count


def _vnext_final_line_capacity_warning_reasons(final_cars: list[dict]) -> list[str]:
    physical = _vnext_physical_module()
    loads: dict[str, float] = defaultdict(float)
    for car in final_cars:
        line = physical.normalize_line(car.get("Line"))
        if line:
            loads[line] += physical.car_length(car)

    reasons: list[str] = []
    for line, load in sorted(loads.items()):
        spec = physical.TRACK_SPECS.get(line)
        if not spec or line in physical.DEPOT_LINES:
            continue
        if load > spec.length_m + physical.LINE_LENGTH_TOLERANCE_M:
            reasons.append(f"{line}:{load:g}>{spec.length_m:g}")
    return reasons


def _vnext_final_cars_from_response_or_payload(*, case_id: str, payload: dict, use_response: bool) -> list[dict]:
    by_no = {
        str(car.get("No") or ""): dict(car)
        for car in payload.get("StartStatus") or []
        if str(car.get("No") or "")
    }
    path = APP_VNEXT_OUTPUT_DIR / "responses" / f"{case_id}.json"
    if not use_response or not path.exists():
        return list(by_no.values())
    response = json.loads(path.read_text(encoding="utf-8"))
    generated = (response.get("Data") or {}).get("GeneratedEndStatus") or []
    for row in generated:
        no = str(row.get("No") or "")
        if not no:
            continue
        car = by_no.setdefault(no, {"No": no})
        car["Line"] = row.get("Line")
        car["Position"] = row.get("Position")
    return list(by_no.values())


def _vnext_closed_door_violation_reasons(blocked_reason: str) -> str:
    return "|".join(
        reason
        for reason in str(blocked_reason or "").split("|")
        if reason.startswith("closed_door_replay")
    )


def _vnext_closed_door_violation_count(blocked_reason: str) -> int:
    reasons = _vnext_closed_door_violation_reasons(blocked_reason)
    return len(_p10_split_pipe(reasons))


def _vnext_load_response_or_build(summary, operation_rows, payload: dict) -> dict:
    response_path = Path(summary.response_path) if summary.response_path else None
    if operation_rows and response_path is not None and response_path.exists():
        return json.loads(response_path.read_text(encoding="utf-8"))
    physical = _vnext_physical_module()
    success = summary.status == "completed"
    return {
        "Success": success,
        "Message": "" if success else summary.blocked_reason,
        "StatusCode": 200 if success else 409,
        "Data": {
            "Operations": [physical.response_operation(row) for row in operation_rows],
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
    solver_kind: str = SOLVER_VNEXT,
) -> None:
    summary_dict = asdict(summary)
    vehicle_display_labels = _p10_vehicle_display_labels(payload)
    status = summary_dict["status"]
    status_text = _p10_status_text(status)
    business_hook_count = _p10_business_hook_count(operation_rows)
    action_counts = _p10_action_counts(operation_rows)
    if status == "completed":
        st.success(f"{solver_kind} 已完成求解并生成接口响应。")
    elif status == "invalid_input":
        st.error(f"输入不符合 {solver_kind} 当前接口要求。")
    else:
        st.warning(f"{solver_kind} 未完成求解，页面展示当前阻塞诊断。")
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
    remote_cols = st.columns(5)
    remote_cols[0].metric("求解策略", summary_dict.get("solve_strategy", "未知"))
    remote_cols[1].metric("远端跨区次数", summary_dict.get("remote_interaction_cross_count", 0))
    remote_cols[2].metric("业务勾切换次数", summary_dict.get("remote_business_transition_count", 0))
    remote_cols[3].metric("远端相关批次", summary_dict.get("remote_interaction_batch_count", 0))
    remote_cols[4].metric("远端会话数", summary_dict.get("remote_interaction_session_count", 0))
    st.caption(
        "口径说明：业务挂摘勾数 = Get/Put 次数，用于和人工勾数比较；"
        "内部移动批次 = runtime 一次取放搬运分组；接口操作数 = Operations 条数，包含称重等非挂摘操作。"
    )

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
        st.json(_p10_response_for_display(response, vehicle_display_labels))
        st.download_button(
            "下载响应 JSON",
            data=json.dumps(response, ensure_ascii=False, indent=2),
            file_name=f"p10_response_{summary_dict['case_id']}.json",
            mime="application/json",
            key="p10-response-download",
        )
    elif view == "批次/操作计划":
        hook_rows = _p10_hook_summary_rows(operation_rows, vehicle_display_labels)
        if hook_rows:
            st.markdown("**按内部移动批次汇总**")
            st.dataframe(hook_rows, width="stretch", hide_index=True)
            st.markdown("**接口操作序列 / 业务挂摘勾号**")
            st.dataframe(_p10_operation_table_rows(operation_rows, vehicle_display_labels), width="stretch", hide_index=True)
        else:
            st.info("当前没有生成操作。")
    elif view == "可视化回放":
        _render_p10_replay(payload, operation_rows, response, vehicle_display_labels, key_prefix="p10")
    elif view == "终态":
        _render_p10_end_status(response, vehicle_display_labels)
    else:
        _render_vnext_diagnostics(summary, candidate_rows, rejection_reasons, vehicle_display_labels)


def _p10_status_text(status: str) -> str:
    return {
        "completed": "完成",
        "blocked": "阻塞",
        "invalid_input": "输入错误",
    }.get(status, status)


def _stringify_value_column(rows: list[dict]) -> list[dict]:
    return [
        {
            **row,
            "value": "" if row.get("value") is None else str(row.get("value")),
        }
        for row in rows
    ]


def _p10_vehicle_display_labels(payload: dict) -> dict[str, str]:
    labels: dict[str, str] = {}
    for car in payload.get("StartStatus") or []:
        no = str(car.get("No") or "").strip()
        if not no:
            continue
        attributes = _p10_vehicle_display_attributes(car)
        labels[no] = f"{no}({';'.join(attributes)})"
    return labels


def _p10_vehicle_display_attributes(car: dict) -> list[str]:
    attributes: list[str] = []
    target_lines = [
        str(line).strip()
        for line in car.get("TargetLines") or []
        if str(line).strip()
    ]
    display_targets = _p10_display_target_lines(target_lines)
    attributes.append("/".join(display_targets) if display_targets else "无目标")

    repair_process = str(car.get("RepairProcess") or "").strip()
    if repair_process:
        attributes.append(repair_process)

    length_text = _p10_format_number(car.get("Length"))
    if length_text:
        attributes.append(f"{length_text}m")

    if _p10_as_bool(car.get("IsHeavy")):
        attributes.append("重")
    if _p10_as_bool(car.get("IsWeigh")):
        attributes.append("称重")
    if _p10_as_bool(car.get("IsClosedDoor")):
        attributes.append("关门")

    force_positions = [
        str(position).strip()
        for position in car.get("ForceTargetPosition") or []
        if str(position).strip()
    ]
    if force_positions:
        attributes.append(f"位{'/'.join(force_positions)}")
    return attributes


def _p10_display_target_lines(target_lines: list[str]) -> list[str]:
    display_targets: list[str] = []
    for line in target_lines:
        display_line = "大库内" if line in {"修1库内", "修2库内", "修3库内", "修4库内"} else line
        if display_line not in display_targets:
            display_targets.append(display_line)
    return display_targets


def _p10_format_number(value) -> str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def _p10_as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "是"}
    return bool(value)


def _p10_vehicle_label(car_no, vehicle_display_labels: dict[str, str]) -> str:
    no = str(car_no or "").strip()
    if not no:
        return ""
    return vehicle_display_labels.get(no, f"{no}(目标:未知)")


def _p10_compact_vehicle_label(car_no, vehicle_display_labels: dict[str, str]) -> str:
    no = str(car_no or "").strip()
    if not no:
        return ""
    full_label = _p10_vehicle_label(no, vehicle_display_labels)
    target_match = re.search(r"\(([^;)]+)", full_label)
    target_text = target_match.group(1) if target_match else "?"
    short_target = target_text.split("/")[0]
    return f"{no}({short_target})"


def _p10_format_vehicle_list(car_nos: list[str], vehicle_display_labels: dict[str, str]) -> str:
    return " ".join(
        label
        for label in (_p10_vehicle_label(car_no, vehicle_display_labels) for car_no in car_nos)
        if label
    )


def _p10_format_vehicle_pipe(text: str, vehicle_display_labels: dict[str, str]) -> str:
    return _p10_format_vehicle_list(_p10_split_pipe(text), vehicle_display_labels)


def _p10_annotate_known_vehicle_text(text, vehicle_display_labels: dict[str, str]) -> str:
    result = str(text or "")
    for car_no, label in sorted(vehicle_display_labels.items(), key=lambda item: len(item[0]), reverse=True):
        result = re.sub(
            rf"(?<![0-9A-Za-z]){re.escape(car_no)}(?![0-9A-Za-z])",
            label,
            result,
        )
    return result


def _p10_annotate_known_vehicle_json(value, vehicle_display_labels: dict[str, str]):
    if isinstance(value, dict):
        return {
            key: _p10_annotate_known_vehicle_json(item, vehicle_display_labels)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_p10_annotate_known_vehicle_json(item, vehicle_display_labels) for item in value]
    if isinstance(value, str):
        return _p10_annotate_known_vehicle_text(value, vehicle_display_labels)
    return value


def _p10_response_for_display(value, vehicle_display_labels: dict[str, str]):
    if isinstance(value, list):
        return [_p10_response_for_display(item, vehicle_display_labels) for item in value]
    if not isinstance(value, dict):
        return value

    result = {}
    for key, item in value.items():
        if key in {"MoveCars", "TrainCars"} and isinstance(item, list):
            result[key] = [
                _p10_vehicle_label(car_no, vehicle_display_labels)
                for car_no in item
            ]
        elif key == "No":
            result[key] = _p10_vehicle_label(item, vehicle_display_labels)
        else:
            result[key] = _p10_response_for_display(item, vehicle_display_labels)
    return result


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


def _p10_hook_summary_rows(operation_rows, vehicle_display_labels: dict[str, str]) -> list[dict[str, object]]:
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
                "moveCars": _p10_format_vehicle_list(move_cars, vehicle_display_labels),
                "hasWeigh": any(row.action == "Weigh" for row in group),
                "businessHookCount": sum(row.action in P10_BUSINESS_HOOK_ACTIONS for row in group),
                "operationCount": len(group),
                "route": " -> ".join(route),
            }
        )
    return rows


def _p10_operation_table_rows(operation_rows, vehicle_display_labels: dict[str, str]) -> list[dict[str, object]]:
    rows = []
    business_hook_no = 0
    for row in sorted(operation_rows, key=lambda item: (item.hook_index, item.operation_index)):
        if row.action in P10_BUSINESS_HOOK_ACTIONS:
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
                "moveCars": _p10_format_vehicle_pipe(row.move_cars, vehicle_display_labels),
                "trainCars": _p10_format_vehicle_pipe(row.train_cars, vehicle_display_labels),
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


def _render_p10_replay(
    payload: dict,
    operation_rows,
    response: dict,
    vehicle_display_labels: dict[str, str],
    key_prefix: str = "p10",
) -> None:
    frames = _p10_build_replay_frames(payload, operation_rows, response)
    if not frames:
        st.info("当前没有可回放状态。")
        return
    vehicle_target_tracks = _p10_vehicle_target_tracks(payload)
    max_frame_index = len(frames) - 1
    frame_key = f"{key_prefix}_replay_frame_index"
    if frame_key not in st.session_state:
        st.session_state[frame_key] = 0
    if int(st.session_state[frame_key]) > max_frame_index:
        st.session_state[frame_key] = max_frame_index
    st.markdown('<div id="p10-replay-anchor"></div>', unsafe_allow_html=True)
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
                    vehicle_display_labels=vehicle_display_labels,
                ),
                unsafe_allow_html=True,
            )
        st.markdown(
            _p10_route_chips_html(frame["path"]),
            unsafe_allow_html=True,
        )
    with side_col:
        st.markdown(
            _p10_replay_detail_html(
                frame,
                frame_index=frame_index,
                frame_count=len(frames),
                vehicle_display_labels=vehicle_display_labels,
            ),
            unsafe_allow_html=True,
        )

    state_rows = _p10_state_table_rows(
        frame["state"],
        highlighted_lines=set(frame["path"]) | {frame["source_line"], frame["target_line"]},
        vehicle_display_labels=vehicle_display_labels,
    )
    if state_rows:
        st.markdown(_p10_cars_table_html(state_rows), unsafe_allow_html=True)


def _p10_build_replay_frames(payload: dict, operation_rows, response: dict) -> list[dict]:
    physical = _vnext_physical_module()
    cars = [physical.normalized_car(car) for car in payload.get("StartStatus") or []]
    state = _p10_state_from_physical_cars(cars, physical)
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
    carried_order: list[str] = []
    business_hook_no = 0
    for row in sorted(operation_rows, key=lambda item: (item.hook_index, item.operation_index)):
        action = row.action
        line = physical.normalize_line(row.line)
        move_cars = _p10_split_pipe(row.move_cars)
        path = _p10_split_pipe(row.passby_path)
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
            train_cars = _p10_split_pipe(row.train_cars) or list(carried_order)
            source_line = line
        elif action == "Weigh":
            train_cars = _p10_split_pipe(row.train_cars) or train_cars or move_cars
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
            train_cars = []
            target_line = line
        state = _p10_state_from_physical_cars(cars, physical)

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


def _p10_replay_detail_html(
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
    rows_html = []
    for label, value, multiline in detail_rows:
        value_html = (
            _p10_vehicle_lines_html(value, vehicle_display_labels)
            if multiline
            else escape(str(value))
        )
        value_class = "p10-detail-value p10-detail-value-multiline" if multiline else "p10-detail-value"
        rows_html.append(
            "<div class='p10-detail-row'>"
            f"<div class='p10-detail-label'>{escape(label)}</div>"
            f"<div class='{value_class}'>{value_html}</div>"
            "</div>"
        )
    return f"""
    <style>
    .p10-detail-panel {{
      border: 1px solid #d9e2ec;
      border-radius: 8px;
      overflow: hidden;
      background: #ffffff;
    }}
    .p10-detail-row {{
      display: grid;
      grid-template-columns: minmax(86px, 34%) minmax(0, 1fr);
      border-bottom: 1px solid #e5edf5;
      min-height: 34px;
    }}
    .p10-detail-row:last-child {{
      border-bottom: 0;
    }}
    .p10-detail-label {{
      padding: 8px 10px;
      background: #f8fafc;
      color: #475569;
      font-size: 13px;
      font-weight: 700;
      line-height: 1.35;
    }}
    .p10-detail-value {{
      padding: 8px 10px;
      color: #0f172a;
      font-size: 13px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }}
    .p10-detail-value-multiline {{
      display: flex;
      flex-direction: column;
      gap: 6px;
      max-height: 360px;
      overflow: auto;
    }}
    .p10-detail-car {{
      display: block;
      padding: 3px 0;
      border-bottom: 1px dashed #e2e8f0;
      overflow-wrap: anywhere;
    }}
    .p10-detail-car:last-child {{
      border-bottom: 0;
    }}
    </style>
    <div class="p10-detail-panel">{''.join(rows_html)}</div>
    """


def _p10_vehicle_lines_html(car_nos: list[str], vehicle_display_labels: dict[str, str]) -> str:
    if not car_nos:
        return "无"
    return "".join(
        f"<span class='p10-detail-car'>{escape(_p10_vehicle_label(car_no, vehicle_display_labels))}</span>"
        for car_no in car_nos
    )


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


def _p10_state_from_physical_cars(cars: list[dict], physical) -> dict[str, list[str]]:
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


def _p10_expand_path_for_map(active_path: list[str], tracks: dict) -> list[str]:
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


def _p10_yard_svg(
    *,
    state_by_line: dict[str, list[str]],
    active_path: set[str],
    source_line: str,
    target_line: str,
    move_cars: list[str],
    train_cars: list[str],
    vehicle_display_labels: dict[str, str],
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
            chip_width = 92
            chip_gap = 4
            max_chips = max(1, int((track_width - 48) // (chip_width + chip_gap)))
            for chip_index, car_no in enumerate(cars[:max_chips]):
                cx = chip_x + chip_index * (chip_width + chip_gap)
                is_active = car_no in move_set
                chip_class = "p10-chip p10-chip-active" if is_active else "p10-chip"
                text_class = "p10-chip-text p10-chip-text-active" if is_active else "p10-chip-text"
                full_label = _p10_vehicle_label(car_no, vehicle_display_labels)
                compact_label = _p10_compact_vehicle_label(car_no, vehicle_display_labels)
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
                    f'<text class="p10-more" x="{chip_x + max_chips * (chip_width + chip_gap) + 2:.1f}" '
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


def _p10_cars_table_html(rows: list[dict[str, object]]) -> str:
    if not rows:
        return ""
    columns = [column for column in ("line", "carCount", "cars") if column in rows[0]]
    header_labels = {
        "line": "股道",
        "carCount": "辆数",
        "cars": "车辆",
    }
    header_html = "".join(
        f"<th class='p10-cars-col-{escape(column)}'>{escape(header_labels.get(column, column))}</th>"
        for column in columns
    )
    body_rows = []
    for row in rows:
        cells = []
        for column in columns:
            value = row.get(column, "")
            if column == "cars":
                value_html = _p10_cars_cell_html(value)
            else:
                value_html = escape(str(value))
            cells.append(f"<td class='p10-cars-col-{escape(column)}'>{value_html}</td>")
        body_rows.append(f"<tr>{''.join(cells)}</tr>")
    return f"""
    <style>
    .p10-cars-table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      margin: 8px 0 14px 0;
      border: 1px solid #d9e2ec;
      border-radius: 8px;
      overflow: hidden;
      font-size: 13px;
    }}
    .p10-cars-table th,
    .p10-cars-table td {{
      border-bottom: 1px solid #e5edf5;
      padding: 7px 9px;
      vertical-align: top;
      line-height: 1.35;
    }}
    .p10-cars-table th {{
      background: #f8fafc;
      color: #475569;
      font-weight: 700;
      text-align: left;
    }}
    .p10-cars-table tr:last-child td {{
      border-bottom: 0;
    }}
    .p10-cars-col-line {{
      width: 112px;
      white-space: nowrap;
    }}
    .p10-cars-col-carCount {{
      width: 64px;
      text-align: right;
      white-space: nowrap;
    }}
    .p10-cars-col-cars {{
      width: auto;
      overflow-wrap: anywhere;
    }}
    .p10-cars-list {{
      display: flex;
      flex-wrap: wrap;
      gap: 5px 8px;
      align-items: flex-start;
    }}
    .p10-cars-item {{
      display: inline-block;
      padding: 2px 6px;
      border-radius: 5px;
      background: #f1f5f9;
      color: #0f172a;
      border: 1px solid #e2e8f0;
      overflow-wrap: anywhere;
    }}
    </style>
    <table class="p10-cars-table">
      <thead><tr>{header_html}</tr></thead>
      <tbody>{''.join(body_rows)}</tbody>
    </table>
    """


def _p10_cars_cell_html(value) -> str:
    if isinstance(value, (list, tuple)):
        items = [str(item) for item in value if str(item)]
    else:
        items = _p10_split_vehicle_labels(str(value))
    if not items:
        return ""
    return "<div class='p10-cars-list'>" + "".join(
        f"<span class='p10-cars-item'>{escape(item)}</span>"
        for item in items
    ) + "</div>"


def _p10_split_vehicle_labels(value: str) -> list[str]:
    text = value.strip()
    if not text:
        return []
    matches = re.findall(r"\S+\([^)]*\)", text)
    return matches or [text]


def _p10_state_table_rows(
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
                    _p10_vehicle_label(car_no, vehicle_display_labels)
                    for car_no in cars
                ],
            }
        )
    return rows


def _render_p10_end_status(response: dict, vehicle_display_labels: dict[str, str]) -> None:
    rows = _p10_end_status_rows(response, vehicle_display_labels)
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
    st.markdown(_p10_cars_table_html(line_rows), unsafe_allow_html=True)
    st.markdown("**终态车辆位置**")
    st.dataframe(rows, width="stretch", hide_index=True)


def _p10_end_status_rows(response: dict, vehicle_display_labels: dict[str, str]) -> list[dict[str, object]]:
    status_rows = ((response or {}).get("Data") or {}).get("GeneratedEndStatus") or []
    rows = [
        {
            "vehicleNo": _p10_vehicle_label(item.get("No"), vehicle_display_labels),
            "line": str(item.get("Line") or ""),
            "position": _p10_int_or_zero(item.get("Position")),
        }
        for item in status_rows
    ]
    return sorted(rows, key=lambda row: (row["line"], row["position"], row["vehicleNo"]))


def _render_vnext_diagnostics(
    summary,
    step_traces,
    rejection_reasons,
    vehicle_display_labels: dict[str, str],
) -> None:
    st.markdown("**vNext CaseSummaryRow**")
    st.json(_p10_annotate_known_vehicle_json(asdict(summary), vehicle_display_labels))

    reason_rows = [
        {"reason": _p10_annotate_known_vehicle_text(reason, vehicle_display_labels), "count": count}
        for reason, count in sorted(rejection_reasons.items(), key=lambda item: (-item[1], item[0]))
    ]
    if reason_rows:
        st.markdown("**拒绝/阻塞原因分布**")
        st.dataframe(reason_rows, width="stretch", hide_index=True)

    trace_dicts = [asdict(row) for row in step_traces]
    if not trace_dicts:
        st.info("当前没有 vNext step trace。")
        return

    selected_filter = st.radio(
        "vNext trace",
        options=["selected", "accepted", "rejected", "all"],
        horizontal=True,
        key="vnext-trace-filter",
    )
    filtered = []
    for row in trace_dicts:
        if selected_filter == "selected" and not row["selected"]:
            continue
        if selected_filter == "accepted" and not row["gate_accepted"]:
            continue
        if selected_filter == "rejected" and row["gate_accepted"]:
            continue
        filtered.append(row)

    display_rows = [
        {
            "hook": row["hook_index"],
            "selected": row["selected"],
            "gateAccepted": row["gate_accepted"],
            "gateReason": row["gate_reason"],
            "phase": row["phase"],
            "family": row["family"],
            "intent": row["intent"],
            "template": row["template_name"],
            "source": row["source_line"],
            "target": row["target_line"],
            "moveCars": _p10_format_vehicle_pipe(row["move_nos"], vehicle_display_labels),
            "contractReduction": row["contract_reduction"],
            "totalReduction": row["total_reduction"],
            "physicalReasons": _p10_annotate_known_vehicle_text(row["physical_reasons"], vehicle_display_labels),
            "resourceViolations": _p10_annotate_known_vehicle_text(row["resource_violations"], vehicle_display_labels),
            "contract": _p10_annotate_known_vehicle_text(row["contract_id"], vehicle_display_labels),
        }
        for row in filtered
    ]
    st.caption(f"当前显示 {len(display_rows)} / {len(trace_dicts)} 条 vNext trace。")
    st.dataframe(display_rows, width="stretch", hide_index=True)


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


def _render_stage1_simple_dashboard() -> None:
    st.subheader("第一阶段可视化")
    st.caption("读取 scripts/stage1_simple 的输出，查看全量完成情况、单案例勾计划、线路回放和终态边界。")
    artifact_text = st.text_input(
        "第一阶段输出目录",
        value=str(DEFAULT_STAGE1_SIMPLE_DIR),
        key="stage1-simple-artifact-dir",
    )
    artifact_dir = Path(artifact_text).expanduser()
    aggregate_path = artifact_dir / "aggregate_summary.json"
    if not aggregate_path.exists():
        st.warning("没有找到 aggregate_summary.json。请先运行 stage1_simple 求解器生成输出。")
        st.code(
            "python3 scripts/stage1_simple/solve.py data/truth2 --out artifacts/stage1_simple_goal_verify",
            language="bash",
        )
        return

    try:
        aggregate = _p10_read_json(aggregate_path)
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
    metric_cols = st.columns(7)
    metric_cols[0].metric("案例数", aggregate.get("cases", len(summaries)))
    metric_cols[1].metric("完成", aggregate.get("complete", "-"))
    metric_cols[2].metric("Partial", aggregate.get("partial", "-"))
    metric_cols[3].metric("平均业务勾", _stage1_average(business_hook_values))
    metric_cols[4].metric("最大业务勾", max(business_hook_values) if business_hook_values else 0)
    metric_cols[5].metric(">40 业务勾", len(over_40))
    metric_cols[6].metric("平均搬运批次", _stage1_average(move_batch_values))
    st.caption(
        "口径说明：现场业务勾数 = Get/Put 次数；搬运批次 = 求解器一次 Get+Put 搬运，"
        "通常等于 2 个业务勾。称重 Weigh 不计入业务勾。"
    )

    filter_cols = st.columns([2, 2, 3])
    status_filter = filter_cols[0].selectbox("状态", ["全部", "complete", "partial"], key="stage1-status-filter")
    min_hooks = filter_cols[1].number_input("最小业务勾数", min_value=0, value=0, step=1, key="stage1-min-hooks")
    case_query = filter_cols[2].text_input("案例/阻塞原因搜索", value="", key="stage1-case-query")
    filtered_rows = _stage1_filter_case_rows(
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
    request_payload = _stage1_load_truth_payload(selected_case)
    if request_payload is None:
        st.warning(f"没有在 data/truth2 中找到 {selected_case} 的原始输入，无法做车辆标签和初始回放。")
        request_payload = {"StartStatus": [], "locoNode": {}}

    selected_cols = st.columns(6)
    debt = summary.get("stage1_debt") or {}
    business_hook_count = _stage1_business_hook_count(response)
    weigh_count = _stage1_action_count(response, "Weigh")
    selected_cols[0].metric("状态", summary.get("status", ""))
    selected_cols[1].metric("业务勾数", business_hook_count)
    selected_cols[2].metric("搬运批次", summary.get("hooks", 0))
    selected_cols[3].metric("债务", debt.get("debt_count", 0))
    selected_cols[4].metric("待编组", len(debt.get("pending_stage1_nos") or []))
    selected_cols[5].metric("称重操作", weigh_count)

    operation_rows = _stage1_response_operation_rows(response)
    vehicle_display_labels = _p10_vehicle_display_labels(request_payload)
    view = st.radio(
        "查看内容",
        options=["可视化回放", "勾计划", "终态", "Trace/诊断", "原始 JSON"],
        horizontal=True,
        key="stage1-view",
    )
    if view == "可视化回放":
        _render_p10_replay(request_payload, operation_rows, response, vehicle_display_labels, key_prefix="stage1")
    elif view == "勾计划":
        hook_rows = _p10_hook_summary_rows(operation_rows, vehicle_display_labels)
        st.markdown("**按搬运批次汇总**")
        st.caption("每个搬运批次通常包含 1 次 Get + 1 次 Put，即 2 个现场业务勾；有称重时中间多 1 次 Weigh。")
        st.dataframe(hook_rows, width="stretch", hide_index=True)
        st.markdown("**接口操作序列（Get/Put 为业务勾）**")
        st.dataframe(_p10_operation_table_rows(operation_rows, vehicle_display_labels), width="stretch", hide_index=True)
    elif view == "终态":
        _render_p10_end_status(response, vehicle_display_labels)
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
            st.json(_p10_response_for_display(response, vehicle_display_labels))


def _stage1_average(values: list[int]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


def _stage1_case_rows(summaries: list[dict], artifact_dir: Path | None = None) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for summary in summaries:
        debt = summary.get("stage1_debt") or {}
        case_id = str(summary.get("case_id") or "")
        response = _stage1_try_load_response(artifact_dir, case_id) if artifact_dir is not None else {}
        business_hooks = _stage1_business_hook_count(response) if response else int(summary.get("operations") or 0)
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


def _stage1_filter_case_rows(
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
        haystack = f"{row.get('caseId')} {row.get('blockingReasons')}".lower()
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


def _stage1_try_load_response(artifact_dir: Path | None, case_id: str) -> dict:
    if artifact_dir is None or not case_id:
        return {}
    path = artifact_dir / f"{case_id}_response.json"
    if not path.exists():
        return {}
    try:
        return _stage1_read_json(path)
    except Exception:  # noqa: BLE001
        return {}


def _stage1_business_hook_count(response: dict) -> int:
    return sum(
        1
        for op in (((response or {}).get("Data") or {}).get("Operations") or [])
        if op.get("Action") in P10_BUSINESS_HOOK_ACTIONS
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
        key: _stage1_read_json(path)
        for key, path in paths.items()
    }


def _stage1_read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _stage1_load_truth_payload(case_id: str) -> dict | None:
    matches = sorted(TRUTH2_DIR.glob(f"*{case_id}.json"))
    if not matches:
        return None
    try:
        return _p10_read_json(matches[0])
    except Exception:  # noqa: BLE001
        return None


def _stage1_response_operation_rows(response: dict) -> list[Stage1OperationRow]:
    operations = ((response or {}).get("Data") or {}).get("Operations") or []
    rows: list[Stage1OperationRow] = []
    hook_index = 0
    for fallback_index, op in enumerate(operations, start=1):
        action = str(op.get("Action") or "")
        if action == "Get":
            hook_index += 1
        elif hook_index <= 0:
            hook_index = 1
        rows.append(
            Stage1OperationRow(
                hook_index=hook_index,
                operation_index=int(op.get("Index") or fallback_index),
                action=action,
                line=str(op.get("Line") or ""),
                move_cars=_stage1_operation_value_to_pipe(op.get("MoveCars")),
                train_cars=_stage1_operation_value_to_pipe(op.get("TrainCars")),
                passby_path=_stage1_operation_value_to_pipe(op.get("PassbyPath")),
            )
        )
    return rows


def _stage1_operation_value_to_pipe(value) -> str:
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
                "move": _p10_format_vehicle_list(row.get("move") or [], vehicle_display_labels),
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
        value=str(DEFAULT_STAGE2_SIMPLE_DIR),
        key="stage2-simple-artifact-dir",
    )
    artifact_dir = Path(artifact_text).expanduser()
    aggregate_path = artifact_dir / "aggregate_summary.json"
    if not aggregate_path.exists():
        st.warning("没有找到 aggregate_summary.json。请先运行 stage2_simple 求解器生成输出。")
        st.code(
            "python3 scripts/stage2_simple/solve.py data/truth2 "
            "--stage1-out artifacts/stage1_simple_initial_depot_done --out artifacts/stage2_simple_final",
            language="bash",
        )
        return

    try:
        aggregate = _p10_read_json(aggregate_path)
    except Exception as exc:  # noqa: BLE001
        st.error(f"读取 aggregate_summary.json 失败：{exc}")
        return

    summaries = list(aggregate.get("summaries") or [])
    if not summaries:
        st.warning("aggregate_summary.json 中没有 summaries。")
        return

    case_rows = _stage2_case_rows(summaries, artifact_dir)
    operation_values = [int(row.get("businessHooks") or 0) for row in case_rows if row.get("status") == "complete"]
    metric_cols = st.columns(7)
    metric_cols[0].metric("案例数", aggregate.get("cases", len(summaries)))
    metric_cols[1].metric("完成", aggregate.get("complete", "-"))
    metric_cols[2].metric("Partial", aggregate.get("partial", "-"))
    metric_cols[3].metric("完成均勾", aggregate.get("avg_operations_complete", _stage1_average(operation_values)))
    metric_cols[4].metric("完成最大勾", aggregate.get("max_operations_complete", max(operation_values) if operation_values else 0))
    metric_cols[5].metric("Stage1未完成", _stage2_reason_count(aggregate, "stage1_not_complete"))
    metric_cols[6].metric("Stage2真阻塞", _stage2_true_partial_count(case_rows))
    st.caption(
        "口径说明：第二阶段按接口操作行计勾，Get/Put 都算 1 勾；本页不处理称重。"
        "Stage2 的回放起点是 stage2_request，即第一阶段求解后的实际状态。"
    )

    filter_cols = st.columns([2, 2, 3])
    status_filter = filter_cols[0].selectbox("状态", ["全部", "complete", "partial"], key="stage2-status-filter")
    min_hooks = filter_cols[1].number_input("最小业务勾数", min_value=0, value=0, step=1, key="stage2-min-hooks")
    case_query = filter_cols[2].text_input("案例/阻塞原因搜索", value="", key="stage2-case-query")
    filtered_rows = _stage1_filter_case_rows(
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
        st.warning(f"案例 {selected_case} 的 stage2_request/response/summary/trace 文件不完整。")
        return

    summary = bundle["summary"]
    response = bundle["response"]
    trace = bundle["trace"]
    request_payload = bundle["stage2_request"]
    display_response = _stage2_response_with_generated(request_payload, response)
    debt = summary.get("stage2_debt") or {}
    operation_rows = _stage1_response_operation_rows(response)
    vehicle_display_labels = _p10_vehicle_display_labels(request_payload)

    selected_cols = st.columns(7)
    selected_cols[0].metric("状态", summary.get("status", ""))
    selected_cols[1].metric("业务勾数", _stage1_business_hook_count(response))
    selected_cols[2].metric("接口操作", summary.get("operations", 0))
    selected_cols[3].metric("待出库", len(debt.get("pending_stage2_nos") or []))
    selected_cols[4].metric("存4新增段", len(debt.get("new_store4_segment") or []))
    selected_cols[5].metric("搜索展开", summary.get("expansions", 0))
    selected_cols[6].metric("耗时秒", summary.get("elapsed_seconds", 0))
    if summary.get("blocking_reasons"):
        st.info("阻塞原因：" + " | ".join(summary.get("blocking_reasons") or []))
    if summary.get("waived_replay_differences"):
        st.caption("提示：waived_replay_differences 是第二阶段口径下允许的存4北通行差异，不计为硬违规。")

    view = st.radio(
        "查看内容",
        options=["可视化回放", "勾计划", "存4新增段", "终态", "Trace/诊断", "原始 JSON"],
        horizontal=True,
        key="stage2-view",
    )
    if view == "可视化回放":
        _render_p10_replay(request_payload, operation_rows, display_response, vehicle_display_labels, key_prefix="stage2")
    elif view == "勾计划":
        st.markdown("**接口操作序列（Get/Put 为业务勾）**")
        st.dataframe(_p10_operation_table_rows(operation_rows, vehicle_display_labels), width="stretch", hide_index=True)
    elif view == "存4新增段":
        _render_stage2_store4_segment(debt, vehicle_display_labels)
    elif view == "终态":
        _render_p10_end_status(display_response, vehicle_display_labels)
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
            st.json(_p10_response_for_display(request_payload, vehicle_display_labels))
            st.markdown("**response**")
            st.json(_p10_response_for_display(response, vehicle_display_labels))


def _stage2_case_rows(summaries: list[dict], artifact_dir: Path | None = None) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for summary in summaries:
        debt = summary.get("stage2_debt") or {}
        case_id = str(summary.get("case_id") or "")
        response = _stage1_try_load_response(artifact_dir, case_id) if artifact_dir is not None else {}
        business_hooks = _stage1_business_hook_count(response) if response else int(summary.get("business_hooks") or 0)
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


def _stage2_true_partial_count(case_rows: list[dict[str, object]]) -> int:
    return sum(
        1
        for row in case_rows
        if row.get("status") == "partial"
        and "stage1_not_complete" not in str(row.get("blockingReasons") or "")
    )


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
        key: _stage1_read_json(path)
        for key, path in paths.items()
    }


def _stage2_response_with_generated(request_payload: dict, response: dict) -> dict:
    try:
        import replay_validator as replay
    except Exception:  # noqa: BLE001
        return response
    replayed, _bad = replay.replay(request_payload, response)
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
                "车辆": _p10_vehicle_label(no, vehicle_display_labels),
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
                "move": _p10_format_vehicle_list(row.get("move") or [], vehicle_display_labels),
                "trainAfter": _p10_format_vehicle_list(row.get("train_after") or [], vehicle_display_labels),
                "path": " -> ".join(row.get("path") or []),
            }
        )
    st.dataframe(rows, width="stretch", hide_index=True)


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
    st.dataframe(stage_rows, width="stretch", hide_index=True)

    failed_distribution = dataset.get("failed_stage_distribution") or {}
    if failed_distribution:
        st.markdown("**失败阶段分布**")
        st.dataframe(
            [{"failedAt": key, "count": value} for key, value in sorted(failed_distribution.items())],
            width="stretch",
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
    st.dataframe(filtered_rows, width="stretch", hide_index=True)
    st.download_button(
        "下载当前明细 CSV",
        data=_rows_to_csv(filtered_rows),
        file_name="l7_eval_case_rows.csv",
        mime="text/csv",
    )

    scenario_names = [str(row["scenario"]) for row in filtered_rows]
    if scenario_names:
        selected_scenario = st.selectbox("选中案例", scenario_names)
        selected_path = ROOT_DIR / "data" / "validation_inputs" / dataset_name / selected_scenario
        st.code(str(selected_path), language="text")


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



if __name__ == "__main__":
    main()
