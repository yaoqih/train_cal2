from __future__ import annotations


RESOURCE_GATES: dict[str, frozenset[str]] = {
    "抛丸线": frozenset({"机南", "机走棚"}),
    "油漆线": frozenset({"洗油北", "机走棚"}),
    "洗罐站": frozenset({"洗罐线北", "洗油北", "机走棚"}),
    "洗罐线北": frozenset({"洗油北", "机走棚"}),
    "机南": frozenset({"机走棚"}),
    "洗油北": frozenset({"机走棚"}),
    "机走棚": frozenset({"机走北"}),
    "调梁棚": frozenset({"调梁线北", "机北2"}),
    "机库线": frozenset({"调梁线北", "机北2"}),
    "存5线南": frozenset({"存5线北"}),
}


def resource_gate_closure(target: str) -> frozenset[str]:
    pending = list(RESOURCE_GATES.get(target, ()))
    closure: set[str] = set()
    while pending:
        line = pending.pop()
        if line in closure:
            continue
        closure.add(line)
        pending.extend(RESOURCE_GATES.get(line, ()))
    return frozenset(closure)
