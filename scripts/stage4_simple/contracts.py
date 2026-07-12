from __future__ import annotations

from solver_vnext import physical

from .domain import (
    ContractGraph,
    DepotRehookContract,
    DepotRehookMode,
    FlowContract,
)
from .search import Stage4Problem


DEPOT_REHOOK_ID = "DEPOT_OUTBOUND_REHOOK"
SERVICE_TARGETS = frozenset({"抛丸线", "洗罐站", "洗罐线北", "油漆线"})


def classify_depot_rehook(problem: Stage4Problem) -> DepotRehookContract:
    cars = problem.cars
    c4 = tuple(physical.line_access_order(cars, "存4线"))
    unload = tuple(physical.line_access_order(cars, "卸轮线"))
    paint = tuple(
        no for no in unload
        if problem.target_by_no.get(no) == "油漆线"
    )
    if not c4 and not paint:
        return DepotRehookContract(
            mode=DepotRehookMode.NOT_REQUIRED,
            c4_backbone=(),
            paint_tail=(),
            unload_prefix=(),
            paint_outbound=(),
        )

    last_paint = max(
        (index for index, no in enumerate(unload) if no in set(paint)),
        default=-1,
    )
    unload_prefix = unload[: last_paint + 1]
    paint_existing = tuple(physical.line_access_order(cars, "油漆线"))
    paint_outbound = tuple(
        no for no in paint_existing
        if no in problem.active_nos
        and problem.target_by_no.get(no) != "油漆线"
    )
    combined = physical.pull_equivalent([
        problem.by_no[no] for no in (*c4, *unload_prefix)
    ])

    if not paint:
        mode = DepotRehookMode.C4_ONLY
    elif combined > physical.PULL_LIMIT_EQUIVALENT:
        mode = DepotRehookMode.BATCHED
    elif unload_prefix != paint:
        mode = DepotRehookMode.PREFIX_DEPENDENCY
    elif paint_outbound:
        mode = DepotRehookMode.OUTBOUND_DEPENDENCY
    elif paint_existing:
        mode = DepotRehookMode.TARGET_REBUILD
    else:
        mode = DepotRehookMode.DIRECT
    return DepotRehookContract(
        mode=mode,
        c4_backbone=c4,
        paint_tail=paint,
        unload_prefix=unload_prefix,
        paint_outbound=paint_outbound,
    )


def build_contract_graph(problem: Stage4Problem) -> ContractGraph:
    rehook = classify_depot_rehook(problem)
    contracts: list[FlowContract] = [FlowContract(
        contract_id=DEPOT_REHOOK_ID,
        subjects=(*rehook.c4_backbone, *rehook.paint_tail),
        target="存4线",
    )]
    targets = sorted({
        problem.target_by_no.get(no, "")
        for no in problem.active_nos
        if problem.target_by_no.get(no)
    })
    for target in targets:
        contract_id = f"TARGET_WINDOW:{target}"
        contracts.append(FlowContract(
            contract_id=contract_id,
            subjects=tuple(sorted(
                no for no in problem.active_nos
                if problem.target_by_no.get(no) == target
            )),
            target=target,
            predecessors=frozenset({DEPOT_REHOOK_ID}),
        ))
    return ContractGraph(tuple(contracts))
