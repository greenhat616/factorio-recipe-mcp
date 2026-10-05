"""Plan files under one root: names, atomic writes, revisions, fingerprints and a trash folder."""

import json
import math
import os
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from ..database import Database
from ..planner.limits import limit_rows
from ..planner.model import TIME, Planner
from ..planner.schema import Defaults, Per, Weights
from .models import (
    Block,
    BlockInput,
    EditOp,
    EditResult,
    Fingerprint,
    PlanFile,
    PlanSummary,
    PlanView,
    Staleness,
    TargetRef,
)
from .ops import apply_ops, line_id

NAME = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')
MAX_BYTES = 1024 * 1024
TRASH = '.trash'


def now() -> str:
    return datetime.now(UTC).isoformat(timespec='seconds')


def check_block(db: Database, plan: PlanFile, block: Block) -> None:
    """The argument and line checks solve_production would make, without solving."""
    req = block.request
    try:
        ids = {b.id for b in plan.blocks}
        p = Planner(
            db,
            plan.force,
            req.validate_stage,
            req.research_productivity,
            req.mining_productivity,
            req.energy_mode,
            req.solar_factor,
            req.wind_factor,
        )
        targets: set[str] = set()
        for name, v in req.targets.items():
            targets.add(p.key(name))
            if isinstance(v, TargetRef):
                for ref in [] if v.from_ == '*' else v.from_:
                    if ref == block.id or ref not in ids:
                        raise ValueError(f'target {name} refers to unknown or own block {ref}')
            elif not math.isfinite(v) or v < 0 or (req.mode == 'maximize' and v <= 0):
                raise ValueError(f'target {name}={v} must be finite and nonnegative (positive with mode=maximize)')
        for name, v in req.consume.items():
            if p.key(name) in targets:
                raise ValueError(f'{name} is both a target and consumed')
            if isinstance(v, TargetRef):
                for ref in [] if v.from_ == '*' else v.from_:
                    if ref == block.id or ref not in ids:
                        raise ValueError(f'consume {name} refers to unknown or own block {ref}')
            elif not math.isfinite(v) or v <= 0:
                raise ValueError(f'consume {name}={v} must be finite and positive')
        if req.solver == 'matrix' and (req.mode == 'maximize' or req.limits or req.integer_machines):
            raise ValueError('mode=maximize, limits and integer_machines need the lp solver')
        auto = (not req.lines) if req.auto_discover is None else req.auto_discover
        if auto and not req.targets:
            raise ValueError('auto-discovery needs targets; pass lines')
        if req.solver == 'matrix' and auto and not req.lines:
            raise ValueError('the matrix solver needs lines')
        weights = Weights.model_validate({'machines': 0, 'power_MW': 0, 'imports': 0, 'surplus': 0, **req.weights})
        if weights.beacons or weights.modules:
            raise ValueError('weights.beacons and weights.modules are reserved and not implemented yet')
        defaults = req.defaults or Defaults()
        for d in (defaults, req.disposal_defaults or Defaults()):
            if d.module_options is not None:
                raise ValueError('module_options is reserved and not implemented yet')
        seen: set[str] = set()
        rows = []
        for spec in req.lines:
            lid = line_id(spec)
            if lid in seen:
                raise ValueError(f'duplicate line id {lid}; give one of the lines an id')
            seen.add(lid)
            row = p.line(spec, defaults)
            if req.validate_stage and row.blocked:
                raise ValueError(f'stage-locked line {lid}: ' + ', '.join(row.blocked))
            rows.append(row)
        if req.limits:
            limit_rows(p, rows, req.limits, TIME[plan.per])
    except (ValueError, ValidationError) as e:
        raise ValueError(f'block {block.id}: {e}') from e


class PlanStore:
    def __init__(self, root: Path, db: Database) -> None:
        self.root, self.db = Path(root), db

    def path(self, name: str) -> Path:
        if not NAME.match(name) or name in ('.', '..'):
            raise ValueError(f'Invalid plan name {name!r}: use 1-64 of A-Z a-z 0-9 _ . -')
        root = self.root.resolve()
        path = (root / f'{name}.json').resolve()
        if path.parent != root:
            raise ValueError(f'Invalid plan name {name!r}')
        return path

    def fingerprint(self) -> Fingerprint:
        prov = self.db.progress.get('provenance', {})
        return Fingerprint(
            prototype_raw_sha256=self.db.raw_sha256,
            progress_tick=self.db.progress.get('tick'),
            source_copy_sha256=prov.get('source_copy_sha256'),
        )

    def staleness(self, fp: Fingerprint) -> Staleness:
        cur = self.fingerprint()
        if fp.prototype_raw_sha256 is None or cur.prototype_raw_sha256 is None:
            return 'unknown'
        if fp.prototype_raw_sha256 != cur.prototype_raw_sha256:
            return 'prototypes_changed'
        if (fp.progress_tick, fp.source_copy_sha256) != (cur.progress_tick, cur.source_copy_sha256):
            return 'stage_changed'
        return 'fresh'

    def exists(self, name: str) -> bool:
        return self.path(name).exists()

    def read(self, name: str) -> PlanFile:
        path = self.path(name)
        if not path.exists():
            raise ValueError(f'No plan {name}; plan_list shows the saved plans')
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            return PlanFile.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as e:
            raise ValueError(f'Plan file {path} is not a valid schema-1 plan; fix or delete it: {e}') from e

    def write(self, plan: PlanFile) -> None:
        path = self.path(plan.name)
        text = plan.model_dump_json(indent=2, by_alias=True, exclude_unset=False)
        if len(text.encode('utf-8')) > MAX_BYTES:
            raise ValueError(f'Plan {plan.name} would exceed {MAX_BYTES} bytes; split it into several plans')
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f'{path.name}.tmp-{os.getpid()}')
        try:
            tmp.write_text(text, encoding='utf-8', newline='\n')
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def validate(self, plan: PlanFile) -> None:
        ids = [b.id for b in plan.blocks]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f'Duplicate block ids: {dupes}')
        for b in plan.blocks:
            if b.enabled:
                check_block(self.db, plan, b)

    def view(self, name: str, include_results: bool = True) -> PlanView:
        plan = self.read(name)
        result_stale = {b.id: self.staleness(b.result.fingerprint) for b in plan.blocks if b.result}
        if not include_results:
            plan = plan.model_copy(update={'blocks': [b.model_copy(update={'result': None}) for b in plan.blocks]})
        return PlanView(plan=plan, stale=self.staleness(plan.fingerprint), result_stale=result_stale)

    def list(self) -> list[PlanSummary]:
        out: list[PlanSummary] = []
        if not self.root.exists():
            return out
        for path in sorted(self.root.glob('*.json')):
            try:
                plan = self.read(path.stem)
            except ValueError:
                continue
            out.append(
                PlanSummary(
                    name=plan.name,
                    description=plan.description,
                    blocks=len(plan.blocks),
                    updated_at=plan.updated_at,
                    revision=plan.revision,
                    stale=self.staleness(plan.fingerprint),
                )
            )
        return out

    def save(
        self,
        name: str,
        blocks: Sequence[BlockInput],
        description: str = '',
        force: str = '',
        per: Per = 'second',
        overwrite: bool = False,
    ) -> PlanView:
        path = self.path(name)
        revision = 0
        if path.exists():
            if not overwrite:
                raise ValueError(f'Plan {name} exists; pass overwrite=true to replace it, or edit it with plan_edit')
            revision = self.read(name).revision
        stamp = now()
        plan = PlanFile(
            name=name,
            description=description,
            revision=revision + 1,
            created_at=stamp,
            updated_at=stamp,
            force=force,
            per=per,
            fingerprint=self.fingerprint(),
            blocks=[Block(id=b.id, description=b.description, enabled=b.enabled, request=b.request) for b in blocks],
        )
        self.validate(plan)
        self.write(plan)
        return self.view(name)

    def commit(self, plan: PlanFile, expected_revision: int | None) -> PlanFile:
        """Write plan as the next revision unless the file moved on since expected_revision."""
        current = self.read(plan.name).revision
        if expected_revision is not None and expected_revision != current:
            raise ValueError(f'Revision conflict: expected {expected_revision}, current {current}; re-read the plan')
        plan = plan.model_copy(update={'revision': current + 1, 'updated_at': now()})
        self.write(plan)
        return plan

    def edit(self, name: str, ops: Sequence[EditOp], expected_revision: int | None = None) -> EditResult:
        plan = self.read(name)
        if expected_revision is not None and expected_revision != plan.revision:
            raise ValueError(
                f'Revision conflict: expected {expected_revision}, current {plan.revision}; re-read the plan'
            )
        edited, notes, warnings = apply_ops(plan, ops, self.db)
        self.validate(edited)
        saved = self.commit(edited, plan.revision)
        return EditResult(plan=name, revision=saved.revision, notes=notes, warnings=warnings)

    def delete(self, name: str, confirm: str) -> str:
        path = self.path(name)
        if confirm != name:
            raise ValueError(f'confirm must equal the plan name {name!r}')
        if not path.exists():
            raise ValueError(f'No plan {name}')
        trash = self.root / TRASH
        trash.mkdir(parents=True, exist_ok=True)
        target = trash / f'{name}-{datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")}.json'
        os.replace(path, target)
        return str(target)
