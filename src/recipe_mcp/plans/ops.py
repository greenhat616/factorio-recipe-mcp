"""plan_edit operations, applied to an in-memory copy; the caller validates and writes only if all succeed."""

from collections.abc import Sequence
from typing import Any

from ..database import Database
from ..planner.schema import LineSpec
from .models import (
    AddBlock,
    AddLine,
    Block,
    BlockRequest,
    EditOp,
    Pin,
    PlanFile,
    RemoveBlock,
    RemoveConsume,
    RemoveLimit,
    RemoveLine,
    RemoveTarget,
    RenameBlock,
    ReplaceModule,
    SetBeacons,
    SetConsume,
    SetEnabled,
    SetLimit,
    SetMeta,
    SetModules,
    SetTarget,
    TargetRef,
    UpdateLine,
    UpdateRequest,
)

SCALAR_LIMITS = {'power_MW', 'machines', 'beacons', 'pollution_per_minute'}
MAPPING_LIMITS = {'imports', 'machines_by_type', 'beacons_by_type', 'modules'}
DEFAULTS = 'defaults'
JSONDict = dict[str, Any]


def line_id(spec: str | LineSpec) -> str:
    return spec if isinstance(spec, str) else spec.id or spec.recipe


def references(block: Block) -> set[str]:
    """Block ids this block's targets name explicitly ('*' is resolved when solving)."""
    out: set[str] = set()
    for v in block.request.targets.values():
        if isinstance(v, TargetRef) and v.from_ != '*':
            out.update(v.from_)
    return out


def request_dict(block: Block) -> JSONDict:
    return block.request.model_dump(exclude_unset=True, by_alias=True)


def set_request(block: Block, data: JSONDict) -> None:
    block.request = BlockRequest.model_validate(data)


def merge(base: JSONDict, fields: JSONDict) -> JSONDict:
    out = dict(base)
    for k, v in fields.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = v
    return out


def spec_dict(spec: str | LineSpec) -> JSONDict:
    return {'recipe': spec} if isinstance(spec, str) else spec.model_dump(exclude_unset=True)


def line_index(block: Block, lid: str) -> int:
    ids = [line_id(s) for s in block.request.lines]
    if lid not in ids:
        raise ValueError(f'No line {lid} in block {block.id}; lines: {ids}')
    return ids.index(lid)


def limit_path(key: str) -> tuple[str, str | None]:
    top, _, sub = key.partition('.')
    if top in SCALAR_LIMITS and not sub:
        return top, None
    if top in MAPPING_LIMITS and sub:
        return top, sub
    raise ValueError(
        f'Unknown limit key {key!r}: use {sorted(SCALAR_LIMITS)} or <{"|".join(sorted(MAPPING_LIMITS))}>.<name>'
    )


def swap_modules(spec: Any, old: str, new: str) -> tuple[Any, bool]:
    """Replace a module in a ModuleSpec (list or {module: count})."""
    if isinstance(spec, dict):
        if old not in spec:
            return spec, False
        out = {k: v for k, v in spec.items() if k != old}
        out[new] = out.get(new, 0) + spec[old]
        return out, True
    if isinstance(spec, list) and old in spec:
        return [new if m == old else m for m in spec], True
    return spec, False


def swap_in_beacons(beacons: Any, old: str, new: str) -> tuple[Any, bool]:
    changed = False
    if not isinstance(beacons, list):
        return beacons, False
    out = []
    for b in beacons:
        b = dict(b)
        b['modules'], hit = swap_modules(b.get('modules', []), old, new)
        changed |= hit
        out.append(b)
    return out, changed


def swap_in_defaults(defaults: Any, old: str, new: str) -> tuple[Any, bool]:
    if not isinstance(defaults, dict):
        return defaults, False
    d = dict(defaults)
    d['modules'], a = swap_modules(d.get('modules', []), old, new)
    d['beacons'], b = swap_in_beacons(d.get('beacons', []), old, new)
    return d, a or b


def apply_ops(plan: PlanFile, ops: Sequence[EditOp], db: Database) -> tuple[PlanFile, list[str], list[str]]:
    plan = plan.model_copy(deep=True)
    notes: list[str] = []
    warnings: list[str] = []
    for i, op in enumerate(ops):
        try:
            apply_one(plan, op, db, notes, warnings)
        except ValueError as e:
            raise ValueError(f'ops[{i}] {op.op}: {e}; nothing was saved') from e
    return plan, notes, warnings


def apply_one(plan: PlanFile, op: EditOp, db: Database, notes: list[str], warnings: list[str]) -> None:
    match op:
        case AddBlock(block=b):
            if any(x.id == b.id for x in plan.blocks):
                raise ValueError(f'block {b.id} exists')
            plan.blocks.append(Block(id=b.id, description=b.description, enabled=b.enabled, request=b.request))
        case RemoveBlock(block_id=bid):
            plan.block(bid)
            users = [x.id for x in plan.blocks if bid in references(x)]
            if users:
                raise ValueError(f'block {bid} is referenced by {users}; change their targets first')
            plan.blocks = [x for x in plan.blocks if x.id != bid]
        case RenameBlock(block_id=bid, new_id=new):
            block = plan.block(bid)
            if any(x.id == new for x in plan.blocks):
                raise ValueError(f'block {new} exists')
            block.id = new
            for x in plan.blocks:
                for v in x.request.targets.values():
                    if isinstance(v, TargetRef) and v.from_ != '*':
                        v.from_ = [new if r == bid else r for r in v.from_]
        case SetEnabled(block_id=bid, enabled=enabled):
            plan.block(bid).enabled = enabled
        case UpdateRequest(block_id=bid, fields=fields):
            block = plan.block(bid)
            set_request(block, merge(request_dict(block), fields))
        case AddLine(block_id=bid, line=line):
            block = plan.block(bid)
            lid = line_id(line)
            if lid in {line_id(s) for s in block.request.lines}:
                raise ValueError(f'line {lid} exists in block {bid}; give the new line another id')
            data = request_dict(block)
            data['lines'] = [*data.get('lines', []), spec_dict(line)]
            set_request(block, data)
        case UpdateLine(block_id=bid, line_id=lid, fields=fields):
            block = plan.block(bid)
            j = line_index(block, lid)
            data = request_dict(block)
            data['lines'][j] = merge(spec_dict(block.request.lines[j]), fields)
            set_request(block, data)
        case RemoveLine(block_id=bid, line_id=lid):
            block = plan.block(bid)
            j = line_index(block, lid)
            data = request_dict(block)
            del data['lines'][j]
            set_request(block, data)
        case SetTarget(block_id=bid, item=item, value=value):
            block = plan.block(bid)
            data = request_dict(block)
            value_data = value.model_dump(by_alias=True) if isinstance(value, TargetRef) else value
            data['targets'] = {**data.get('targets', {}), item: value_data}
            set_request(block, data)
        case RemoveTarget(block_id=bid, item=item):
            block = plan.block(bid)
            data = request_dict(block)
            if item not in data.get('targets', {}):
                raise ValueError(f'no target {item} in block {bid}; targets: {sorted(data.get("targets", {}))}')
            del data['targets'][item]
            set_request(block, data)
        case SetLimit(block_id=bid, key=key, value=value):
            block = plan.block(bid)
            top, sub = limit_path(key)
            data = request_dict(block)
            limits = dict(data.get('limits') or {})
            if sub is None:
                limits[top] = value
            else:
                limits[top] = {**limits.get(top, {}), sub: value}
            data['limits'] = limits
            set_request(block, data)
        case RemoveLimit(block_id=bid, key=key):
            block = plan.block(bid)
            top, sub = limit_path(key)
            data = request_dict(block)
            limits = dict(data.get('limits') or {})
            present = top in limits if sub is None else sub in limits.get(top, {})
            if not present:
                raise ValueError(f'no limit {key} in block {bid}')
            if sub is None:
                del limits[top]
            else:
                limits[top] = {k: v for k, v in limits[top].items() if k != sub}
            data['limits'] = limits
            set_request(block, data)
        case SetConsume(block_id=bid, item=item, value=value):
            block = plan.block(bid)
            data = request_dict(block)
            data['consume'] = {**data.get('consume', {}), item: value}
            set_request(block, data)
        case RemoveConsume(block_id=bid, item=item):
            block = plan.block(bid)
            data = request_dict(block)
            if item not in data.get('consume', {}):
                raise ValueError(f'no consume {item} in block {bid}')
            del data['consume'][item]
            set_request(block, data)
        case Pin(block_id=bid, solver=solver):
            block = plan.block(bid)
            r = block.result
            if r is None or r.status != 'optimal' or not r.lines_for_matrix:
                raise ValueError(f'block {bid} has no optimal result to pin; run plan_solve first')
            data = request_dict(block)
            data['lines'] = [s.model_dump(exclude_unset=True) for s in r.lines_for_matrix]
            data['auto_discover'] = False
            if r.matrix_args is not None:
                data['imports'] = r.matrix_args.imports
                data['surplus_items'] = r.matrix_args.surplus_items
            if solver is not None:
                data['solver'] = solver
            set_request(block, data)
            notes.append(f'pinned {len(r.lines_for_matrix)} lines in block {bid}')
        case SetMeta(description=description, force=force, per=per):
            if description is not None:
                plan.description = description
            if force is not None:
                plan.force = force
            if per is not None and per != plan.per:
                warnings.append(f'per changed {plan.per} -> {per}; stored rates were not converted')
                plan.per = per
        case SetModules(block_id=bid, line_id=lid, modules=modules):
            set_slot(plan.block(bid), lid, 'modules', modules)
        case SetBeacons(block_id=bid, line_id=lid, beacons=beacons):
            dumped = None if beacons is None else [b.model_dump(exclude_unset=True) for b in beacons]
            set_slot(plan.block(bid), lid, 'beacons', dumped)
        case ReplaceModule(from_=old, to=new, block_id=only):
            if new not in db.raw.get('module', {}):
                raise ValueError(f'unknown module {new}')
            total = 0
            for block in [plan.block(only)] if only else plan.blocks:
                lines, defaults = replace_in_block(block, old, new)
                total += lines
                notes.append(
                    f'replace_module {old} -> {new}: block {block.id}: {lines} lines'
                    + (', defaults' if defaults else '')
                )
                total += defaults
            if total == 0:
                warnings.append(f'replace_module: no {old} found')


def set_slot(block: Block, lid: str, key: str, value: Any) -> None:
    data = request_dict(block)
    if lid == DEFAULTS:
        if key == 'modules' and isinstance(value, dict):
            raise ValueError('defaults.modules is a priority list of module names, not {module: count}')
        defaults = dict(data.get('defaults') or {})
        if value is None:
            defaults.pop(key, None)
        else:
            defaults[key] = value
        data['defaults'] = defaults
    else:
        j = line_index(block, lid)
        spec = spec_dict(block.request.lines[j])
        if value is None:
            spec.pop(key, None)
        else:
            spec[key] = value
        data['lines'][j] = spec
    set_request(block, data)


def replace_in_block(block: Block, old: str, new: str) -> tuple[int, int]:
    """(lines changed, defaults changed) after replacing module old with new everywhere in the block."""
    data = request_dict(block)
    changed = 0
    lines = []
    for spec in block.request.lines:
        d = spec_dict(spec)
        d['modules'], a = swap_modules(d.get('modules'), old, new)
        d['beacons'], b = swap_in_beacons(d.get('beacons'), old, new)
        if d['modules'] is None:
            del d['modules']
        if d['beacons'] is None:
            del d['beacons']
        changed += a or b
        lines.append(d)
    if lines:
        data['lines'] = lines
    defaults_changed = 0
    for key in ('defaults', 'disposal_defaults'):
        if key in data:
            data[key], hit = swap_in_defaults(data[key], old, new)
            defaults_changed += hit
    set_request(block, data)
    return changed, defaults_changed
