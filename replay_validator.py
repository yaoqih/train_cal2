#!/usr/bin/env python3
from __future__ import annotations

import argparse
import heapq
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOCO = 15.0
TOL = 0.5
PULL_LIMIT = 20
DEPOT = {f"修{i}库内" for i in range(1, 5)}
DEPOT_OUT = {f"修{i}库外" for i in range(1, 5)}
RUNNING = {"联6", "联7"} | {f"渡{i}" for i in range(1, 14)}
WEIGH = "机库线"
SPOTTING_TOTAL = {"调梁棚": 11, "洗罐站": 7, "油漆线": 9, "抛丸线": 3}
TRACK_LEN = {
    "机北1": 81.4, "存1线": 113.0, "存2线": 239.2, "存3线": 258.5, "存4线": 317.8,
    "存4南": 154.5, "存5线北": 367.0, "存5线南": 156.0, "机北2": 55.7, "机库线": 71.6,
    "调梁线北": 70.1, "调梁棚": 174.3, "机走北": 69.1, "机走棚": 111.1,
    "预修线": 208.5, "洗油北": 62.9, "机南": 90.1, "洗罐线北": 100.0,
    "洗罐站": 88.7, "抛丸线": 42.3, "油漆线": 109.0, "卸轮线": 47.3,
    "修1库外": 49.3, "修1库内": 151.7, "修2库外": 49.3, "修2库内": 151.7,
    "修3库外": 49.3, "修3库内": 151.7, "修4库外": 49.3, "修4库内": 151.7,
}
EDGES = (
    ("修4库内", "修4库外"), ("修3库内", "修3库外"), ("修2库内", "修2库外"), ("修1库内", "修1库外"),
    ("修4库外", "渡13"), ("修3库外", "渡13"), ("修2库外", "渡12"), ("修1库外", "渡11"), ("卸轮线", "渡11"),
    ("渡13", "渡12"), ("渡12", "联7"), ("渡11", "联7"), ("抛丸线", "渡10"), ("联7", "渡10"),
    ("渡10", "渡9"), ("渡10", "机南"), ("渡9", "渡8"), ("渡8", "存4南"), ("渡8", "存5线南"),
    ("渡9", "预修线"), ("洗罐站", "洗罐线北"), ("洗罐线北", "洗油北"), ("油漆线", "洗油北"),
    ("机南", "机走棚"), ("洗油北", "机走棚"), ("调梁棚", "调梁线北"), ("机库线", "渡4"),
    ("预修线", "存2线"), ("预修线", "渡7"), ("调梁线北", "渡4"), ("存5线南", "存5线北"),
    ("存4南", "存4线"), ("存4南", "存3线"), ("渡7", "存1线"), ("渡7", "渡6"),
    ("机走北", "渡5"), ("机走棚", "机走北"), ("渡6", "渡5"), ("渡4", "机北2"),
    ("存5线北", "渡1"), ("存4线", "渡1"), ("存3线", "渡3"), ("存2线", "渡3"),
    ("存1线", "机北1"), ("机北2", "机北1"), ("渡5", "机北2"), ("机北1", "渡2"),
    ("渡3", "渡2"), ("渡1", "联6"), ("渡2", "联6"),
)
APPROACH = {
    "修4库外": ("渡13",), "修3库外": ("渡13",), "修2库外": ("渡12",), "修1库外": ("渡11",),
    "卸轮线": ("渡11",), "抛丸线": ("渡10",), "存5线南": ("存5线北",), "存5线北": ("渡1",),
    "存4南": ("存4线", "存3线"), "存4线": ("渡1",), "存3线": ("渡3",), "存2线": ("渡3",),
    "存1线": ("机北1",), "机北1": ("渡2",), "机北2": ("机北1",), "机走北": ("渡5",),
    "调梁线北": ("渡4",), "机库线": ("渡4",), "机走棚": ("机走北",), "预修线": ("渡7", "存2线"),
    "机南": ("机走棚",), "洗油北": ("机走棚",), "洗罐线北": ("洗油北",),
}
REV_IGNORE = (
    (("调梁线北", "渡4", "机库线"), (("机北2", 41.5), ("机北1", 97.2))),
    (("渡5", "机北2", "渡4"), (("机北2", 0.0), ("机北1", 55.7))),
    (("机北2", "机北1", "存1线"), (("机北1", 0.0),)),
    (("渡6", "渡5", "机走北"), (("机北2", 40.6), ("机北1", 96.3))),
)
REV_WITH = (
    (("存2线", "预修线", "渡7"), (("预修线", 208.5),)),
    (("存1线", "渡7", "渡6"), (("预修线", 253.9),)),
    (("存4线", "存4南", "存3线"), (("存4南", 154.5),)),
)
ALIASES = {
    "机走北1线": "机北1", "机走北2线": "机北2", "机北3": "机走北", "机走线南": "机南",
    "洗罐油漆北": "洗油北", "洗北": "洗罐线北", "洗南": "洗罐站", "调梁线北": "调梁线北",
    "调北": "调梁线北", "调棚": "调梁棚", "存4北": "存4线", "存4": "存4线", "存4线南": "存4南",
    "存5北": "存5线北", "存5南": "存5线南", "联6线": "联6", "联7线": "联7",
    "机库": "机库线", "库": "机库线", "修1": "修1库内", "修1外": "修1库外",
    "修2": "修2库内", "修2外": "修2库外", "修3": "修3库内", "修3外": "修3库外",
    "修4": "修4库内", "修4外": "修4库外", "预修": "预修线", "存1": "存1线",
    "存2": "存2线", "存3": "存3线", "机棚": "机走棚", "机走": "机走棚",
    "洗": "洗罐站", "油": "油漆线", "抛": "抛丸线", "轮": "卸轮线",
}


@dataclass(frozen=True)
class V:
    index: int
    kind: str
    code: str
    detail: str = ""


@dataclass(frozen=True)
class Slot:
    line: str
    pos: int
    locked: bool = False


def norm(x: Any) -> str:
    s = str(x or "").strip().replace("線", "线")
    return ALIASES.get(s, s)


ADJ: dict[str, set[str]] = defaultdict(set)
for a, b in EDGES:
    ADJ[norm(a)].add(norm(b))
    ADJ[norm(b)].add(norm(a))


def read(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Replay API Operations and validate physical/business rules.")
    p.add_argument("input", help="response JSON, request JSON, or a bundle containing both")
    p.add_argument("--request", help="request JSON when input is response-only")
    p.add_argument("--response", help="response JSON when input is request-only")
    p.add_argument("--max-violations", type=int, default=200)
    return p.parse_args()


def pick_payloads(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any], list[V]]:
    root = read(args.input)
    req = read(args.request) if args.request else None
    resp = read(args.response) if args.response else None
    req = req or first_dict(root, ("Request", "Input", "Payload")) or (root if "StartStatus" in root else None)
    resp = resp or first_dict(root, ("Response", "Output", "Result")) or (root if ("Data" in root or "Operations" in root) else None)
    bad: list[V] = []
    if not isinstance(req, dict) or "StartStatus" not in req:
        bad.append(V(0, "schema", "request_missing", "need StartStatus/TerminalLines/locoNode or --request"))
        req = {}
    if not isinstance(resp, dict) or not operations(resp):
        bad.append(V(0, "schema", "operations_missing", "need Data.Operations/Operations or --response"))
        resp = {}
    return req, resp, bad


def first_dict(root: Any, keys: tuple[str, ...]) -> dict[str, Any] | None:
    if not isinstance(root, dict):
        return None
    for key in keys:
        value = root.get(key)
        if isinstance(value, dict):
            return value
    return None


def operations(resp: dict[str, Any]) -> list[dict[str, Any]]:
    data = resp.get("Data") if isinstance(resp.get("Data"), dict) else resp
    return data.get("Operations") or []


def generated(resp: dict[str, Any]) -> list[dict[str, Any]]:
    data = resp.get("Data") if isinstance(resp.get("Data"), dict) else resp
    return data.get("GeneratedEndStatus") or []


def car_no(c: dict[str, Any]) -> str:
    return str(c.get("No") or c.get("_No") or "")


def ncar(c: dict[str, Any]) -> dict[str, Any]:
    return {
        **c,
        "No": str(c.get("No") or ""),
        "Line": norm(c.get("Line")),
        "Position": int(c.get("Position") or 0),
        "Length": float(c.get("Length") or 14.3),
        "IsHeavy": bool(c.get("IsHeavy")),
        "IsWeigh": bool(c.get("IsWeigh")),
        "IsClosedDoor": bool(c.get("IsClosedDoor")),
        "_Weighed": bool(c.get("_Weighed")),
        "_InitialLine": norm(c.get("_InitialLine") or c.get("Line")),
        "TargetLines": [norm(x) for x in c.get("TargetLines") or []],
        "_Force": tuple(int(x) for x in c.get("ForceTargetPosition") or [] if int(x) > 0),
    }


def length(cars: list[dict[str, Any]], nos: set[str]) -> float:
    return sum(c["Length"] for c in cars if car_no(c) in nos)


def pull(cars: list[dict[str, Any]], nos: set[str]) -> int:
    return sum(4 if c.get("IsHeavy") else 1 for c in cars if car_no(c) in nos)


def line_order(cars: list[dict[str, Any]], line: str, exclude: set[str] = set()) -> list[str]:
    xs = [c for c in cars if c["Line"] == line and car_no(c) not in exclude]
    return [car_no(c) for c in sorted(xs, key=lambda c: (int(c["Position"]), car_no(c)))]


def occupied(cars: list[dict[str, Any]], moving: set[str]) -> set[str]:
    return {c["Line"] for c in cars if c["Line"] and car_no(c) not in moving}


def has_stationary(cars: list[dict[str, Any]], line: str, moving: set[str]) -> bool:
    return any(c["Line"] == line and car_no(c) not in moving for c in cars)


def access(line: str) -> set[str]:
    return {norm(x) for x in APPROACH.get(line, ())}


def route_exists(start: str, end: str) -> bool:
    q, seen = [(0, start)], {start}
    while q:
        _, x = heapq.heappop(q)
        if x == end:
            return True
        for y in ADJ.get(x, ()):
            if y not in seen:
                seen.add(y)
                heapq.heappush(q, (len(seen), y))
    return False


def same_triplet(path: list[str], triplet: tuple[str, str, str]) -> bool:
    t = [norm(x) for x in triplet]
    r = list(reversed(t))
    return any(path[i:i + 3] == t or path[i:i + 3] == r for i in range(max(0, len(path) - 2)))


def rev_ok(triplet: tuple[str, str, str], block: str, cars: list[dict[str, Any]], moving: set[str], train_len: float) -> bool:
    need = train_len + LOCO
    nt = tuple(norm(x) for x in triplet)
    for rules, with_len in ((REV_IGNORE, False), (REV_WITH, True)):
        for rt, limits in rules:
            rt = tuple(norm(x) for x in rt)
            if nt not in (rt, tuple(reversed(rt))):
                continue
            for b, limit in limits:
                if norm(b) != block:
                    continue
                blen = sum(c["Length"] for c in cars if c["Line"] == block and car_no(c) not in moving)
                return blen <= 0 or need + (blen if with_len else 0) <= limit + TOL
    return False


def can_enter_occupied(node: str, nxt: str, path: list[str], cars: list[dict[str, Any]], moving: set[str], train_len: float) -> bool:
    return any(
        z != node and z not in path and rev_ok((node, nxt, z), nxt, cars, moving, train_len)
        for z in ADJ.get(nxt, ())
    )


def reachable_route(action: str, start: str, end: str, cars: list[dict[str, Any]],
                    moving: set[str], train_len: float) -> tuple[list[str], list[str]]:
    dep = access(start) if has_stationary(cars, start, moving) else set()
    app = access(end) if action == "Get" or has_stationary(cars, end, moving) else set()
    occ, endpoints = occupied(cars, moving), {start, end}
    rejected: Counter[str] = Counter()
    queue: list[tuple[int, int, str | None, str, list[str]]] = [(0, 0, None, start, [start])]
    seen = {(None, start, start)}
    seq = 1
    while queue:
        dist, _, prev, node, path = heapq.heappop(queue)
        if node == end:
            return path, []
        for nxt in sorted(ADJ.get(node, ())):
            if nxt in path:
                continue
            if node == start and dep and nxt not in dep:
                rejected[f"source_gate:{start}->{nxt}:allowed={','.join(sorted(dep))}"] += 1
                continue
            if nxt == end and app and node not in app:
                rejected[f"target_gate:{node}->{end}:allowed={','.join(sorted(app))}"] += 1
                continue
            if nxt == "联6" and train_len + LOCO > 192.0 + TOL:
                rejected[f"line_length:联6:{train_len + LOCO:.1f}>192.0"] += 1
                continue
            if node in occ and node not in endpoints and (prev is None or not rev_ok((prev, node, nxt), node, cars, moving, train_len)):
                rejected[f"occupied_reversal:{node}"] += 1
                continue
            if nxt in occ and nxt not in endpoints and not can_enter_occupied(node, nxt, path, cars, moving, train_len):
                rejected[f"occupied_line:{nxt}"] += 1
                continue
            state = (node, nxt, "|".join(path))
            if state in seen:
                continue
            seen.add(state)
            heapq.heappush(queue, (dist + 1, seq, node, nxt, [*path, nxt]))
            seq += 1
    return [], [name for name, _ in rejected.most_common(12)]


def route_errors(index: int, action: str, path0: list[Any], starts0: set[str] | str, end: str, cars: list[dict[str, Any]],
                 moving: set[str], train_len: float) -> list[V]:
    path = [norm(x) for x in path0 if norm(x)]
    starts = {starts0} if isinstance(starts0, str) else set(starts0)
    start = path[0] if path and path[0] in starts else sorted(starts)[0]
    bad: list[V] = []
    found, blockers = next(
        ((p, b) for p, b in (reachable_route(action, item, end, cars, moving, train_len) for item in sorted(starts)) if p),
        ([], []),
    )
    if not found:
        bad.append(V(index, "physical", "route_unreachable", f"{sorted(starts)}->{end}; blockers={blockers}"))
    if not path:
        return [*bad, V(index, "physical", "path_missing", f"{sorted(starts)}->{end}")]
    if path[0] not in starts:
        bad.append(V(index, "physical", "path_start_mismatch", f"{path[0]} not in {sorted(starts)}"))
    if path[-1] != end:
        bad.append(V(index, "physical", "path_end_mismatch", f"{path[-1]}!={end}"))
    if not route_exists(start, end):
        bad.append(V(index, "physical", "route_missing", f"{start}->{end}"))
    for a, b in zip(path, path[1:]):
        if b not in ADJ.get(a, set()):
            bad.append(V(index, "physical", "path_edge_missing", f"{a}->{b}"))
    dep = access(start) if has_stationary(cars, start, moving) else set()
    if dep and len(path) > 1 and path[1] not in dep:
        bad.append(V(index, "physical", "occupied_source_wrong_departure", f"{start}:next={path[1]} allowed={sorted(dep)}"))
    app = access(end) if action == "Get" or has_stationary(cars, end, moving) else set()
    if app and len(path) > 1 and path[-2] not in app:
        bad.append(V(index, "physical", "occupied_target_wrong_approach", f"{end}:prev={path[-2]} allowed={sorted(app)}"))
    occ = occupied(cars, moving)
    for i, x in enumerate(path[1:-1], 1):
        if x in occ and not rev_ok((path[i - 1], x, path[i + 1]), x, cars, moving, train_len):
            bad.append(V(index, "physical", "occupied_line_in_path", x))
    for x in path:
        if x == "联6" and train_len + LOCO > 192.0 + TOL:
            bad.append(V(index, "physical", "route_line_length_violation", f"联6:{train_len + LOCO:.1f}>192.0"))
    for rules, with_len in ((REV_IGNORE, False), (REV_WITH, True)):
        for tri, limits in rules:
            if not same_triplet(path, tri):
                continue
            for b, limit in limits:
                b = norm(b)
                blen = sum(c["Length"] for c in cars if c["Line"] == b and car_no(c) not in moving)
                need = train_len + LOCO + (blen if with_len else 0)
                if blen > 0 and need > limit + TOL:
                    bad.append(V(index, "physical", "route_reversal_length_violation", f"{'/'.join(tri)}:{b}:{need:.1f}>{limit:.1f}"))
    return bad


def compact(cars: list[dict[str, Any]], line: str) -> None:
    if line in DEPOT | DEPOT_OUT or line in SPOTTING_TOTAL:
        return
    for i, c in enumerate(sorted([c for c in cars if c["Line"] == line], key=lambda c: (c["Position"], car_no(c))), 1):
        c["Position"] = i


def apply_get(cars: list[dict[str, Any]], line: str, move: list[str]) -> None:
    for c in cars:
        if car_no(c) in set(move):
            c["Line"], c["Position"] = "", 0
    compact(cars, line)


def forced_put_positions(cars: list[dict[str, Any]], line: str, move: list[str]) -> dict[str, int]:
    by = {car_no(c): c for c in cars}
    old_pos = {int(c["Position"]) for c in cars if c["Line"] == line and car_no(c) not in set(move)}
    used, pos = set(old_pos), {}
    for no in move:
        c = by.get(no)
        if not c or line not in c["TargetLines"] or not c["_Force"]:
            return {}
        free = [p for p in sorted(c["_Force"]) if p not in used]
        if not free:
            return {}
        pos[no] = free[0]
        used.add(free[0])
    return pos if not old_pos or max(pos.values()) < min(old_pos) else {}


def apply_put(cars: list[dict[str, Any]], line: str, move: list[str]) -> None:
    old = [x for x in line_order(cars, line) if x not in set(move)]
    pos = forced_put_positions(cars, line, move) or {no: i for i, no in enumerate(move + old, 1)}
    for c in cars:
        no = car_no(c)
        if no in pos:
            c["Line"], c["Position"] = line, pos[no]


def put_loco_positions(path0: list[Any], line: str) -> set[str]:
    path = [norm(x) for x in path0 if norm(x)]
    out = {line} | access(line)
    if len(path) >= 2 and path[-1] == line:
        out.add(path[-2])
    elif path == [line]:
        app = access(line)
        if len(app) == 1:
            out |= app
    return out


def replay(req: dict[str, Any], resp: dict[str, Any]) -> tuple[list[dict[str, Any]], list[V]]:
    cars = [ncar(c) for c in req.get("StartStatus") or []]
    by = {car_no(c): c for c in cars}
    bad = input_errors(req, cars)
    loco = {norm((req.get("locoNode") or {}).get("Line"))}
    carried: list[str] = []
    for op in sorted(operations(resp), key=lambda x: int(x.get("Index") or 0)):
        idx = int(op.get("Index") or 0)
        action, line = str(op.get("Action") or ""), norm(op.get("Line"))
        move = [str(x) for x in op.get("MoveCars") or []]
        train = [str(x) for x in op.get("TrainCars") or []]
        if action not in {"Get", "Put", "Weigh"}:
            bad.append(V(idx, "physical", "unknown_action", action)); continue
        if line not in TRACK_LEN and line not in RUNNING:
            bad.append(V(idx, "physical", "line_unknown", line))
        if action in {"Get", "Put"} and line in RUNNING:
            bad.append(V(idx, "physical", "running_line_stop_violation", line))
        if any(no not in by for no in move):
            bad.append(V(idx, "physical", "move_car_unknown", ",".join(no for no in move if no not in by)))
            continue
        if action == "Get":
            mset = set(move)
            if any(by[no]["Line"] != line for no in move):
                bad.append(V(idx, "physical", "get_line_mismatch", f"{line}:{move}"))
            if set(carried) & mset:
                bad.append(V(idx, "physical", "get_duplicate_carried", ",".join(sorted(set(carried) & mset))))
            if line_order(cars, line, set(carried))[:len(move)] != move:
                bad.append(V(idx, "physical", "line_end_get_order_violation", f"reachable={line_order(cars, line, set(carried))[:len(move)]}:move={move}"))
            if pull(cars, set(carried) | mset) > PULL_LIMIT:
                bad.append(V(idx, "physical", "pull_limit_violation", f"{pull(cars, set(carried) | mset)}>{PULL_LIMIT}"))
            bad += route_errors(idx, action, op.get("PassbyPath") or [], loco, line, cars, set(carried) | mset, length(cars, set(carried)))
            carried += [no for no in move if no not in carried]
            apply_get(cars, line, move)
            loco = {line}
        elif action == "Put":
            mset = set(move)
            if not mset <= set(carried):
                bad.append(V(idx, "physical", "put_without_carry", ",".join(sorted(mset - set(carried)))))
            if carried[-len(move):] != move if move else False:
                bad.append(V(idx, "physical", "train_tail_put_order_violation", f"tail={carried[-len(move):]} move={move} train={carried}"))
            bad += route_errors(idx, action, op.get("PassbyPath") or [], loco, line, cars, set(carried), length(cars, set(carried)))
            after_len = sum(c["Length"] for c in cars if c["Line"] == line and car_no(c) not in mset) + length(cars, mset)
            if line in TRACK_LEN and after_len > TRACK_LEN[line] + TOL:
                bad.append(V(idx, "physical", "target_line_length_violation", f"{line}:{after_len:.1f}>{TRACK_LEN[line]:.1f}"))
            bad += closed_door_put_errors(idx, line, [by[no] for no in carried if no in by], move, cars)
            apply_put(cars, line, move)
            carried = [no for no in carried if no not in mset]
            loco = put_loco_positions(op.get("PassbyPath") or [], line)
        else:
            if line != WEIGH:
                bad.append(V(idx, "physical", "weigh_line_invalid", line))
            if len(move) != 1 or not set(move) <= set(carried):
                bad.append(V(idx, "physical", "weigh_car_not_carried", ",".join(move)))
            if move and (not carried or carried[-1] != move[0]):
                bad.append(V(idx, "physical", "weigh_car_not_tail", f"tail={carried[-1:]}, move={move}"))
            for no in move:
                if no in by and (not by[no].get("IsWeigh") or by[no].get("_Weighed")):
                    bad.append(V(idx, "physical", "weigh_car_not_pending", no))
            blockers = [car_no(c) for c in cars if c["Line"] == WEIGH and car_no(c) not in set(carried)]
            if blockers:
                bad.append(V(idx, "physical", "weigh_line_not_empty", ",".join(blockers)))
            bad += route_errors(idx, action, op.get("PassbyPath") or [], loco, WEIGH, cars, set(carried), length(cars, set(carried)))
            for no in move:
                by[no]["_Weighed"] = True
            loco = {WEIGH}
        if train != carried:
            bad.append(V(idx, "physical", "train_cars_mismatch", f"expected={carried} actual={train}"))
    if carried:
        bad.append(V(0, "physical", "dirty_train_after_last_operation", ",".join(carried)))
    bad += generated_errors(resp, cars)
    return cars, bad


def input_errors(req: dict[str, Any], cars: list[dict[str, Any]]) -> list[V]:
    bad: list[V] = []
    for key in ("StartStatus", "TerminalLines", "locoNode"):
        if key not in req:
            bad.append(V(0, "schema", f"{key}_missing"))
    nos = [car_no(c) for c in cars]
    for no, n in Counter(nos).items():
        if not no or n > 1:
            bad.append(V(0, "schema", "car_no_duplicate_or_empty", no))
    for c in cars:
        if c["Line"] not in TRACK_LEN:
            bad.append(V(0, "schema", "start_line_unknown", f"{car_no(c)}:{c['Line']}"))
        if not c["TargetLines"]:
            bad.append(V(0, "schema", "target_missing", car_no(c)))
    for line, ps in positions_by_line(cars).items():
        dup = [p for p, n in Counter(ps).items() if p > 0 and n > 1]
        if dup:
            bad.append(V(0, "schema", "initial_position_collision", f"{line}:{dup}"))
    return bad


def positions_by_line(cars: list[dict[str, Any]]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = defaultdict(list)
    for c in cars:
        out[c["Line"]].append(int(c["Position"]))
    return out


def closed_door_put_errors(idx: int, line: str, train: list[dict[str, Any]], move: list[str], cars: list[dict[str, Any]]) -> list[V]:
    if not any(c.get("IsClosedDoor") for c in cars):
        return []
    if line == "存4线":
        projected = [dict(c) for c in cars]
        apply_put(projected, line, move)
        return [V(idx, "business", "closed_door_cun4_put_position_violation", f"{car_no(c)}:{c['Position']}")
                for c in projected if c["Line"] == "存4线" and c.get("IsClosedDoor") and car_no(c) in set(move) and int(c["Position"]) <= 3]
    if train and train[0].get("IsClosedDoor") and (len(train) > 10 or any(c.get("IsHeavy") for c in train)):
        return [V(idx, "business", "closed_door_full_consist_first_car_violation", car_no(train[0]))]
    return []


def generated_errors(resp: dict[str, Any], cars: list[dict[str, Any]]) -> list[V]:
    if not generated(resp):
        return []
    by = {car_no(c): c for c in cars}
    bad: list[V] = []
    for row in generated(resp):
        no, line, pos = str(row.get("No") or ""), norm(row.get("Line")), int(row.get("Position") or 0)
        c = by.get(no)
        if not c:
            bad.append(V(0, "state", "generated_unknown_car", no))
        elif (c["Line"], int(c["Position"])) != (line, pos):
            bad.append(V(0, "state", "generated_end_status_mismatch", f"{no}:replay={c['Line']}#{c['Position']} generated={line}#{pos}"))
    return bad


def final_cars(resp: dict[str, Any], replayed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = generated(resp)
    if not rows:
        return replayed
    cars = [dict(c) for c in replayed]
    by = {car_no(c): c for c in cars}
    if any(str(row.get("No") or "") not in by for row in rows):
        return replayed
    for row in rows:
        c = by[str(row.get("No") or "")]
        c["Line"] = norm(row.get("Line"))
        c["Position"] = int(row.get("Position") or 0)
    return cars


def capacities(req: dict[str, Any]) -> dict[str, int]:
    out = {line: 5 for line in DEPOT}
    for x in req.get("TerminalLines") or []:
        line = norm(x.get("Line"))
        if line in DEPOT:
            out[line] = 7 if x.get("IsInspectionMode") else 5
    return out


def line_no(line: str) -> int:
    return int(line[1]) if line in DEPOT else 0


def repair(c: dict[str, Any]) -> str:
    return str(c.get("RepairProcess") or "")


def slot_ok(c: dict[str, Any], line: str, pos: int, caps: dict[str, int]) -> bool:
    if pos < 1 or pos > caps.get(line, 5):
        return False
    if c["_Force"] and pos not in c["_Force"]:
        return False
    if c["Length"] >= 17.6 and line_no(line) not in {3, 4}:
        return False
    if repair(c).startswith("厂") and pos not in {4, 5}:
        return False
    return True


def build_slots(cars: list[dict[str, Any]], caps: dict[str, int]) -> tuple[dict[str, Slot], dict[str, str]]:
    slots: dict[str, Slot] = {}
    fail: dict[str, str] = {}
    used: dict[str, set[int]] = defaultdict(set)
    depot_cars = [c for c in cars if DEPOT & set(c["TargetLines"])]
    for c in sorted(depot_cars, key=lambda x: (x["_InitialLine"], x["Position"], car_no(x))):
        if c["_InitialLine"] in DEPOT and c["_InitialLine"] in c["TargetLines"] and slot_ok(c, c["_InitialLine"], int(c["Position"]), caps):
            slots[car_no(c)] = Slot(c["_InitialLine"], int(c["Position"]), True)
            used[c["_InitialLine"]].add(int(c["Position"]))
    rest = [c for c in depot_cars if car_no(c) not in slots]
    cand = {car_no(c): [(l, p) for l in sorted(DEPOT & set(c["TargetLines"])) for p in range(1, caps[l] + 1)
                        if p not in used[l] and slot_ok(c, l, p, caps)] for c in rest}
    owner: dict[tuple[str, int], str] = {}
    lookup = {car_no(c): c for c in rest}

    def pref(no: str, s: tuple[str, int]) -> tuple[int, int, int, str]:
        c, (l, p) = lookup[no], s
        return ((line_no(l) not in ({3, 4} if c["Length"] >= 17.6 else {1, 2})),
                0 if not repair(c).startswith("厂") or p in {4, 5} else 1, p, l)

    def assign(no: str, seen: set[tuple[str, int]]) -> bool:
        for s in sorted(cand.get(no, ()), key=lambda x: pref(no, x)):
            if s in seen:
                continue
            seen.add(s)
            if s not in owner or assign(owner[s], seen):
                owner[s] = no
                return True
        return False

    for no in sorted(cand, key=lambda n: (len(cand[n]), n)):
        if not cand[no] or not assign(no, set()):
            fail[no] = "no_feasible_depot_slot"
    for (l, p), no in owner.items():
        if no not in fail:
            slots[no] = Slot(l, p)
    return slots, fail


def business_errors(req: dict[str, Any], cars: list[dict[str, Any]]) -> list[V]:
    bad: list[V] = []
    caps = capacities(req)
    slots, fail = build_slots([ncar(c) for c in req.get("StartStatus") or []], caps)
    by_start = {car_no(ncar(c)): ncar(c) for c in req.get("StartStatus") or []}
    for line, load in line_loads(cars).items():
        if line in TRACK_LEN and load > TRACK_LEN[line] + TOL:
            bad.append(V(0, "business", "final_line_length_violation", f"{line}:{load:.1f}>{TRACK_LEN[line]:.1f}"))
    for c in cars:
        no, line, pos = car_no(c), c["Line"], int(c["Position"])
        start = by_start.get(no, c)
        if no in fail:
            bad.append(V(0, "business", "depot_assignment_failure", f"{no}:{fail[no]}"))
        if c.get("IsWeigh") and not c.get("_Weighed"):
            bad.append(V(0, "business", "weigh_not_completed", no))
        if no in slots and slots[no].locked and (line, pos) != (slots[no].line, slots[no].pos):
            bad.append(V(0, "business", "locked_depot_stayer_moved", f"{no}:{line}#{pos} expected={slots[no].line}#{slots[no].pos}"))
        if line not in start["TargetLines"]:
            bad.append(V(0, "business", "target_line_unsatisfied", f"{no}:{line} not in {start['TargetLines']}"))
            continue
        if line in DEPOT and not slot_ok(start, line, pos, caps):
            bad.append(V(0, "business", "depot_slot_rule_violation", f"{no}:{line}#{pos}"))
        elif start["_Force"] and line in SPOTTING_TOTAL and not spotting_ok(c, cars):
            bad.append(V(0, "business", "spotting_window_unsatisfied", f"{no}:{line}#{pos} force={start['_Force']}"))
        elif start["_Force"] and line not in DEPOT and line not in SPOTTING_TOTAL and pos not in start["_Force"]:
            bad.append(V(0, "business", "force_position_unsatisfied", f"{no}:{line}#{pos} not in {start['_Force']}"))
        if c.get("IsClosedDoor") and line == "存4线" and pos <= 3:
            bad.append(V(0, "business", "closed_door_cun4_position_violation", f"{no}:{pos}"))
    return bad


def spotting_ok(car: dict[str, Any], cars: list[dict[str, Any]]) -> bool:
    line, forced = car["Line"], tuple(car.get("_Force") or ())
    total = SPOTTING_TOTAL.get(line, 0)
    mask = tuple(sorted(p for p in forced if 1 <= p <= total))
    if not total or not mask or mask != tuple(range(mask[0], mask[-1] + 1)):
        return False
    same = [
        c for c in cars
        if c["Line"] == line and line in c.get("TargetLines", ()) and tuple(c.get("_Force") or ()) == forced
    ]
    if not same or len(same) > len(mask):
        return False
    positions = {int(c["Position"]) for c in same}
    south = max(positions)
    suffix = sum(
        1 for c in cars
        if c["Line"] == line and int(c["Position"]) > south
        and not (line in c.get("TargetLines", ()) and tuple(c.get("_Force") or ()) == forced)
    )
    return positions <= set(range(mask[0], total - suffix + 1))


def line_loads(cars: list[dict[str, Any]]) -> Counter[str]:
    out: Counter[str] = Counter()
    for c in cars:
        out[c["Line"]] += c["Length"]
    return out


def summarize(violations: list[V]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    priority = {"schema": 0, "physical": 1, "business": 2, "state": 3}
    first = min(violations, key=lambda v: (priority.get(v.kind, 9), v.index, v.code), default=None)
    groups: dict[tuple[str, str], list[V]] = defaultdict(list)
    for v in violations:
        groups[(v.kind, v.code)].append(v)
    reasons = [
        {
            "kind": kind,
            "code": code,
            "count": len(items),
            "examples": [item.detail for item in items[:3] if item.detail],
        }
        for (kind, code), items in sorted(groups.items(), key=lambda kv: (-len(kv[1]), priority.get(kv[0][0], 9), kv[0][1]))
    ]
    primary = {} if first is None else {"kind": first.kind, "code": first.code, "index": first.index, "detail": first.detail}
    return primary, reasons


def main() -> int:
    args = parse_args()
    req, resp, schema_bad = pick_payloads(args)
    cars, replay_bad = ([], []) if schema_bad else replay(req, resp)
    state_warn = [v for v in replay_bad if v.kind == "state"]
    replay_bad = [v for v in replay_bad if v.kind != "state"]
    biz_bad = [] if schema_bad else business_errors(req, final_cars(resp, cars))
    violations = schema_bad + replay_bad + biz_bad
    physical = [v for v in violations if v.kind == "physical"]
    business = [v for v in violations if v.kind == "business"]
    schema = [v for v in violations if v.kind == "schema"]
    primary, reasons = summarize(violations)
    _warn_primary, warn_reasons = summarize(state_warn)
    out = {
        "ok": not violations,
        "schema_ok": not schema,
        "physical_ok": not physical,
        "business_ok": not business,
        "state_consistency_ok": not state_warn,
        "operation_count": len(operations(resp)) if resp else 0,
        "violation_count": len(violations),
        "warning_count": len(state_warn),
        "primary_blocker": primary,
        "blocking_reasons": reasons[:20],
        "state_warnings": warn_reasons[:10],
        "violations": [v.__dict__ for v in violations[:args.max_violations]],
        "warnings": [v.__dict__ for v in state_warn[:args.max_violations]],
        "truncated": len(violations) > args.max_violations,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
