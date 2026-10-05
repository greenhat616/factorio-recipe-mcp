"""Semantic flow graphs built only from solved rates, never from requested shortfalls."""

from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from typing import Any

from .schema import GraphEdge, GraphNode, GraphOptions, Per, PlanLine, PlanResult, ProductionGraph


def options(value: GraphOptions | Mapping[str, Any] | None) -> GraphOptions:
    result = value if isinstance(value, GraphOptions) else GraphOptions.model_validate(value or {})
    if result.format is not None:
        raise ValueError('graph_options.format is reserved and not implemented yet')
    return result


class Builder:
    def __init__(self, per: Per) -> None:
        self.per = per
        self.nodes: dict[str, GraphNode] = {}
        self.edges: list[GraphEdge] = []

    def node(self, node_id: str, kind: str, label: str, **data: Any) -> str:
        self.nodes[node_id] = GraphNode(id=node_id, kind=kind, label=label, data=data)
        return node_id

    def flow(self, source: str, target: str, key: str, rate: float) -> None:
        if rate <= 1e-9:
            return
        unit = 'MW' if key.startswith('energy:') else f'per {self.per}'
        self.node('item:' + key, 'item', key, key=key, unit=unit)
        self.edges.append(GraphEdge(source=source, target=target, item=key, rate=rate, unit=unit))

    def endpoint(self, kind: str, key: str, rate: float, incoming: bool) -> None:
        if rate <= 1e-9:
            return
        id = self.node(f'{kind}:{key}', kind, key)
        item = 'item:' + key
        self.flow(id if incoming else item, item if incoming else id, key, rate)

    def line(self, row: PlanLine, disposal: bool = False) -> None:
        id = self.node(
            'line:' + row.id,
            'disposal' if disposal else 'line',
            row.recipe,
            **row.model_dump(
                include={
                    'recipe',
                    'machine',
                    'machine_type',
                    'machines',
                    'machines_ceil',
                    'modules',
                    'beacons',
                    'beacon_count',
                    'power_MW',
                    'fuel',
                    'fuel_per_machine',
                    'productivity_model',
                }
            ),
        )
        for k, v in row.inputs.items():
            self.flow('item:' + k, id, k, v)
        for k, v in row.outputs.items():
            self.flow(id, 'item:' + k, k, v)

    def finish(self, allocate: bool = False) -> ProductionGraph:
        graph = ProductionGraph(nodes=list(self.nodes.values()), edges=self.edges)
        reverse: defaultdict[str, list[str]] = defaultdict(list)
        for edge in self.edges:
            if edge.kind == 'flow':
                reverse[edge.target].append(edge.source)
        roots = [n.id for n in graph.nodes if n.kind in ('target', 'factory-out')]
        queue = deque((id, 0) for id in roots)
        while queue:
            id, depth = queue.popleft()
            node = self.nodes[id]
            if node.depth is not None:
                continue
            node.depth = depth
            queue.extend((parent, depth + 1) for parent in reverse[id])
        queue.extend((n.id, 0) for n in graph.nodes if n.depth is None and n.kind in ('surplus', 'dispose', 'disposal'))
        while queue:
            id, depth = queue.popleft()
            node = self.nodes[id]
            if node.depth is not None:
                continue
            node.depth = depth
            queue.extend((parent, depth + 1) for parent in reverse[id])
        for node in graph.nodes:
            if node.kind != 'item':
                continue
            ins = [e for e in self.edges if e.kind == 'flow' and e.target == node.id]
            outs = [e for e in self.edges if e.kind == 'flow' and e.source == node.id]
            amount = sum(e.rate for e in ins)
            error = amount - sum(e.rate for e in outs)
            if abs(error) > 1e-6:
                graph.notes.append(f'{node.id}: flow residual {error:.9g} {node.data["unit"]}')
            if allocate and amount > 1e-9:
                for source in ins:
                    for target in outs:
                        graph.edges.append(
                            GraphEdge(
                                source=source.source,
                                target=target.target,
                                item=source.item,
                                rate=target.rate * source.rate / amount,
                                unit=source.unit,
                                kind='allocated',
                                allocated=True,
                            )
                        )
        if allocate:
            graph.notes.append('Allocated edges assume proportional mixing; they are not physical belt connections.')
        return graph


def build_line_graph(result: PlanResult, opts: GraphOptions) -> ProductionGraph:
    b = Builder(result.per)
    for row in result.lines:
        b.line(row)
    disposal: defaultdict[str, float] = defaultdict(float)
    for row in result.disposal:
        b.line(row, disposal=True)
        for k, v in row.inputs.items():
            disposal[k] += v
    for item in result.items:
        key = item.item
        b.endpoint('import', key, item.imported, True)
        b.endpoint('supply', key, item.supplied, True)
        b.endpoint('target', key, item.target, False)
        b.endpoint('surplus', key, max(0, item.surplus - disposal[key]), False)
    return b.finish(opts.allocate)


def prefix_graph(graph: ProductionGraph, prefix: str) -> ProductionGraph:
    return ProductionGraph(
        nodes=[n.model_copy(update={'id': prefix + n.id}) for n in graph.nodes],
        edges=[e.model_copy(update={'source': prefix + e.source, 'target': prefix + e.target}) for e in graph.edges],
        notes=list(graph.notes),
    )


def factory_graph(
    per: Per,
    outcomes: Sequence[Any],
    results: Mapping[str, PlanResult],
    ledger: Any,
    links: Sequence[GraphEdge],
    full: bool,
    opts: GraphOptions,
) -> ProductionGraph:
    b = Builder(per)
    for outcome in outcomes:
        r = results.get(outcome.id)
        id = b.node(
            'block:' + outcome.id,
            'block',
            outcome.id,
            status=outcome.status,
            totals=r.totals.model_dump() if r else {},
            error=outcome.error,
        )
        if r is None:
            continue
        for item in r.items:
            b.flow(id, 'item:' + item.item, item.item, item.target + item.surplus)
            b.flow('item:' + item.item, id, item.item, item.imported + item.supplied)
    disposed: defaultdict[str, float] = defaultdict(float)
    for row in ledger.disposal:
        for k, v in row.inputs.items():
            disposed[k] += v
    for k, v in ledger.net_inputs.items():
        b.endpoint('factory-in', k, v, True)
    for k, v in ledger.net_outputs.items():
        b.endpoint('factory-out', k, max(0, v - disposed[k]), False)
        b.endpoint('dispose', k, disposed[k], False)
    b.edges.extend(links)
    graph = b.finish(opts.allocate)
    if full:
        for bid, result in results.items():
            child = prefix_graph(build_line_graph(result, opts), f'block:{bid}/')
            b.nodes['block:' + bid].data['children'] = [n.id for n in child.nodes]
            graph.nodes.extend(child.nodes)
            graph.edges.extend(child.edges)
            graph.notes.extend(child.notes)
    return graph
