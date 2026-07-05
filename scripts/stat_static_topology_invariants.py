#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

from solver_vnext import physical as p


OUT_DIR = Path("artifacts/static_topology_invariants")
SCENARIOS = (
    ("source_cleared_target_empty", False, False),
    ("source_cleared_target_occupied", False, True),
    ("source_remains_target_empty", True, False),
    ("source_remains_target_occupied", True, True),
)


def natural_key(value: str) -> tuple[Any, ...]:
    head = "".join(ch for ch in value if not ch.isdigit())
    digits = "".join(ch for ch in value if ch.isdigit())
    return (head, int(digits) if digits else -1, value)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def graph() -> dict[str, set[str]]:
    adj: dict[str, set[str]] = defaultdict(set)
    for left, right in p.LINE_GRAPH_EDGES:
        adj[left].add(right)
        adj[right].add(left)
    return adj


def bfs_distance(
    adj: dict[str, set[str]],
    source: str,
    target: str,
    *,
    source_remains: bool,
    target_occupied: bool,
) -> int | None:
    if source == target:
        return 0
    source_allowed = set(p.OCCUPIED_LINE_APPROACH_LINES.get(source, adj[source])) if source_remains else set(adj[source])
    target_allowed = set(p.OCCUPIED_LINE_APPROACH_LINES.get(target, adj[target])) if target_occupied else set(adj[target])
    q = deque([(source, 0)])
    seen = {source}
    while q:
        node, dist = q.popleft()
        for nxt in sorted(adj[node], key=natural_key):
            if node == source and nxt not in source_allowed:
                continue
            if nxt == target and node not in target_allowed:
                continue
            if nxt in seen:
                continue
            if nxt == target:
                return dist + 1
            seen.add(nxt)
            q.append((nxt, dist + 1))
    return None


def shortest_paths(
    adj: dict[str, set[str]],
    source: str,
    target: str,
    *,
    source_remains: bool,
    target_occupied: bool,
) -> list[list[str]]:
    dist = bfs_distance(adj, source, target, source_remains=source_remains, target_occupied=target_occupied)
    if dist is None:
        return []
    source_allowed = set(p.OCCUPIED_LINE_APPROACH_LINES.get(source, adj[source])) if source_remains else set(adj[source])
    target_allowed = set(p.OCCUPIED_LINE_APPROACH_LINES.get(target, adj[target])) if target_occupied else set(adj[target])
    paths: list[list[str]] = []

    def walk(path: list[str]) -> None:
        node = path[-1]
        if len(path) - 1 == dist:
            if node == target:
                paths.append(path[:])
            return
        for nxt in sorted(adj[node], key=natural_key):
            if nxt in path:
                continue
            if node == source and nxt not in source_allowed:
                continue
            if nxt == target and node not in target_allowed:
                continue
            path.append(nxt)
            walk(path)
            path.pop()

    walk([source])
    return paths


def reversal_rules() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for family, rules in (
        ("ignore_blocker_length", p.REVERSAL_RULES_IGNORE_BLOCKER_LENGTH),
        ("with_blocker_length", p.REVERSAL_RULES_WITH_BLOCKER_LENGTH),
    ):
        for index, (triplet, blockers) in enumerate(rules, 1):
            rows.append({
                "rule_id": f"{family}_{index}",
                "family": family,
                "triplet": ">".join(triplet),
                "blockers": "|".join(f"{line}:{distance:g}" for line, distance in blockers),
            })
    return rows


def matched_reversal_rules(path: list[str], rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    triples = {tuple(path[i:i + 3]) for i in range(len(path) - 2)}
    triples |= {tuple(reversed(path[i:i + 3])) for i in range(len(path) - 2)}
    return [rule for rule in rules if tuple(rule["triplet"].split(">")) in triples]


def path_features(path: list[str], running_nodes: set[str], rules: list[dict[str, Any]]) -> dict[str, Any]:
    matched = matched_reversal_rules(path, rules)
    intermediate_lines = [node for node in path[1:-1] if node in p.TRACK_SPECS]
    explicit_throats = [node for node in path if node in running_nodes]
    endpoint_restricted = [
        endpoint for endpoint in (path[0], path[-1])
        if endpoint in p.OCCUPIED_LINE_APPROACH_LINES
    ]
    reversal_blockers: list[str] = []
    for rule in matched:
        for item in str(rule["blockers"]).split("|"):
            if item:
                reversal_blockers.append(item.split(":", 1)[0])
    limited_route_nodes = [node for node in path if node in p.ROUTE_LINE_LENGTH_LIMITS_M]
    resources = sorted(set(explicit_throats + intermediate_lines + reversal_blockers + limited_route_nodes), key=natural_key)
    cost = (
        (len(path) - 1)
        + 2 * len(explicit_throats)
        + 4 * len(intermediate_lines)
        + 8 * len(matched)
        + 3 * len(endpoint_restricted)
        + 5 * len(limited_route_nodes)
    )
    return {
        "edge_count": len(path) - 1,
        "explicit_throats": "|".join(explicit_throats),
        "explicit_throat_count": len(explicit_throats),
        "intermediate_lines": "|".join(intermediate_lines),
        "intermediate_line_count": len(intermediate_lines),
        "reversal_rule_ids": "|".join(rule["rule_id"] for rule in matched),
        "reversal_rule_count": len(matched),
        "reversal_blockers": "|".join(sorted(set(reversal_blockers), key=natural_key)),
        "endpoint_approach_restricted_count": len(endpoint_restricted),
        "route_length_limited_nodes": "|".join(limited_route_nodes),
        "route_length_limited_count": len(limited_route_nodes),
        "conflict_resources": "|".join(resources),
        "priority_weight": cost,
    }


def empty_path_features() -> dict[str, Any]:
    return {
        "edge_count": "",
        "explicit_throats": "",
        "explicit_throat_count": 0,
        "intermediate_lines": "",
        "intermediate_line_count": 0,
        "reversal_rule_ids": "",
        "reversal_rule_count": 0,
        "reversal_blockers": "",
        "endpoint_approach_restricted_count": 0,
        "route_length_limited_nodes": "",
        "route_length_limited_count": 0,
        "conflict_resources": "",
        "priority_weight": "",
    }


def topology_class(line: str, degree: int, approach_count: int) -> str:
    if degree == 1:
        return "尽头线"
    if approach_count > 1:
        return "多入口贯通/分歧线"
    return "单入口贯通/串联线"


def main() -> None:
    adj = graph()
    all_nodes = sorted(adj, key=natural_key)
    track_lines = list(p.TRACK_SPECS)
    running_nodes = set(all_nodes) - set(track_lines)
    rules = reversal_rules()

    node_rows = []
    for node in all_nodes:
        spec = p.TRACK_SPECS.get(node)
        node_rows.append({
            "node": node,
            "node_kind": "track_line" if spec else "running_throat",
            "track_type": spec.track_type if spec else "running",
            "length_m": spec.length_m if spec else "",
            "degree": len(adj[node]),
            "neighbors": "|".join(sorted(adj[node], key=natural_key)),
        })

    line_rows = []
    for line, spec in p.TRACK_SPECS.items():
        approaches = tuple(p.OCCUPIED_LINE_APPROACH_LINES.get(line, ()))
        degree = len(adj[line])
        cls = topology_class(line, degree, len(approaches))
        line_rows.append({
            "line": line,
            "full_name": p.LINE_FULL_NAMES.get(line, line),
            "track_type": spec.track_type,
            "length_m": spec.length_m,
            "graph_degree": degree,
            "graph_neighbors": "|".join(sorted(adj[line], key=natural_key)),
            "occupied_allowed_approaches": "|".join(approaches),
            "occupied_allowed_approach_count": len(approaches),
            "topology_class": cls,
            "can_be_intermediate_when_empty": degree > 1,
            "blocks_route_when_occupied_as_intermediate": degree > 1,
            "order_mode": "北端栈",
            "queue_capable_under_current_rules": False,
            "depot_assembly_role": line in p.DEPOT_INBOUND_ASSEMBLY_LINES,
            "strategy_note": "倒序放置" if cls != "尽头线" else "单端倒序放置",
        })

    edge_rows = [
        {
            "left": left,
            "right": right,
            "left_kind": "track_line" if left in p.TRACK_SPECS else "running_throat",
            "right_kind": "track_line" if right in p.TRACK_SPECS else "running_throat",
        }
        for left, right in p.LINE_GRAPH_EDGES
    ]

    od_rows: list[dict[str, Any]] = []
    batch_counter: Counter[tuple[str, str]] = Counter()
    resource_to_ods: dict[str, set[str]] = defaultdict(set)
    for source in track_lines:
        for target in track_lines:
            if source == target:
                continue
            for scenario, source_remains, target_occupied in SCENARIOS:
                paths = shortest_paths(
                    adj,
                    source,
                    target,
                    source_remains=source_remains,
                    target_occupied=target_occupied,
                )
                if not paths:
                    od_rows.append({
                        "scenario": scenario,
                        "source_after_get": "remains_occupied" if source_remains else "cleared",
                        "target_before_put": "occupied" if target_occupied else "empty",
                        "source": source,
                        "target": target,
                        "path_status": "no_feasible_path",
                        "path_rank": 0,
                        "shortest_path_count": 0,
                        "first_hop": "",
                        "last_hop": "",
                        "path": "",
                        **empty_path_features(),
                    })
                    continue
                for rank, path in enumerate(paths, 1):
                    features = path_features(path, running_nodes, rules)
                    first_hop = path[1] if len(path) > 1 else ""
                    last_hop = path[-2] if len(path) > 1 else ""
                    row = {
                        "scenario": scenario,
                        "source_after_get": "remains_occupied" if source_remains else "cleared",
                        "target_before_put": "occupied" if target_occupied else "empty",
                        "source": source,
                        "target": target,
                        "path_status": "reachable",
                        "path_rank": rank,
                        "shortest_path_count": len(paths),
                        "first_hop": first_hop,
                        "last_hop": last_hop,
                        "path": ">".join(path),
                        **features,
                    }
                    od_rows.append(row)
                    if rank == 1:
                        throat_signature = features["explicit_throats"]
                        batch_counter[(scenario, throat_signature)] += 1
                        od_key = f"{source}->{target}"
                        for resource in features["conflict_resources"].split("|"):
                            if resource:
                                resource_to_ods[resource].add(od_key)

    cost_rows = [
        row for row in od_rows
        if row["path_rank"] in (0, 1)
    ]
    cost_rows.sort(key=lambda r: (
        r["path_status"] != "no_feasible_path",
        -int(r["priority_weight"] or 0),
        r["scenario"],
        r["source"],
        r["target"],
    ))

    throat_rows = []
    for resource, ods in sorted(resource_to_ods.items(), key=lambda item: (-len(item[1]), natural_key(item[0]))):
        throat_rows.append({
            "resource": resource,
            "resource_kind": "running_throat" if resource in running_nodes else "track_line_or_reversal_blocker",
            "od_count": len(ods),
            "example_ods": "|".join(sorted(ods)[:25]),
        })

    batch_rows = []
    for (scenario, signature), count in sorted(batch_counter.items(), key=lambda item: (-item[1], item[0][0], item[0][1])):
        batch_rows.append({
            "scenario": scenario,
            "throat_signature": signature,
            "od_count": count,
        })

    summary = {
        "source": "scripts/solver_vnext/physical.py",
        "track_line_count": len(track_lines),
        "running_throat_count": len(running_nodes),
        "edge_count": len(p.LINE_GRAPH_EDGES),
        "scenario_count": len(SCENARIOS),
        "od_scenario_count": len(track_lines) * (len(track_lines) - 1) * len(SCENARIOS),
        "od_path_rows": len(od_rows),
        "od_no_path_rows": sum(1 for row in od_rows if row["path_status"] == "no_feasible_path"),
        "od_cost_matrix_rows": len(cost_rows),
        "topology_class_counts": Counter(row["topology_class"] for row in line_rows),
        "reversal_rule_count": len(rules),
        "running_throats": sorted(running_nodes, key=natural_key),
        "cost_formula": "edge_count + 2*explicit_throat_count + 4*intermediate_line_count + 8*reversal_rule_count + 3*endpoint_approach_restricted_count + 5*route_length_limited_count",
        "scenario_boundary": "source_cleared/source_remains 指取车后起点线是否还留有既有车；target_empty/target_occupied 指放车前目标线是否已有车。只有 source_remains 和 target_occupied 会触发北端入口限制。",
        "order_mode_boundary": "所有停车/作业线按北端摘挂，顺序策略均按栈处理；贯通只影响空线可通过和进路选择，不代表可从两端队列式摘挂。",
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    write_csv(OUT_DIR / "topology_nodes.csv", node_rows)
    write_csv(OUT_DIR / "track_line_invariants.csv", line_rows)
    write_csv(OUT_DIR / "graph_edges.csv", edge_rows)
    write_csv(OUT_DIR / "reversal_rules.csv", rules)
    write_csv(OUT_DIR / "od_feasible_paths.csv", od_rows)
    write_csv(OUT_DIR / "od_cost_matrix.csv", cost_rows)
    write_csv(OUT_DIR / "conflict_resource_usage.csv", throat_rows)
    write_csv(OUT_DIR / "shared_route_batches.csv", batch_rows)
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=dict), encoding="utf-8")


if __name__ == "__main__":
    main()
