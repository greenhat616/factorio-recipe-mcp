"""Plan file schema (version 1), block requests, result summaries and edit operations."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..models import Spec
from ..planner.schema import (
    BeaconSpec,
    Defaults,
    DisposalMode,
    Limits,
    LineSpec,
    MatrixArgs,
    Mode,
    ModuleCount,
    ModuleSpec,
    Objective,
    Per,
    PlanResult,
    PlanStatus,
    SolverName,
    Totals,
    WeightName,
)

ID_PATTERN = r'^[A-Za-z0-9_.-]{1,64}$'
Staleness = Literal['fresh', 'stage_changed', 'prototypes_changed', 'unknown']


class TargetRef(Spec):
    """A target that equals what other blocks import of the same item, plus a constant."""

    model_config = ConfigDict(extra='forbid', populate_by_name=True)

    from_: list[str] | Literal['*'] = Field(alias='from', description='Block ids, or "*" for every other enabled block')
    plus: float = 0.0


TargetValue = float | TargetRef


class BlockRequest(Spec):
    """solve_production arguments without force and per, which come from the plan."""

    targets: dict[str, TargetValue] = {}
    lines: list[str | LineSpec] = []
    solver: SolverName = 'lp'
    defaults: Defaults | None = None
    auto_discover: bool | None = None
    imports: list[str] = []
    forbid_imports: list[str] = []
    import_costs: dict[str, float] = {}
    allow_surplus: bool = True
    surplus_items: list[str] = []
    objective: Objective = 'balanced'
    weights: dict[WeightName, float] = {}
    exclude_recipes: list[str] = []
    max_depth: int = 12
    max_lines: int = 1000
    include_hidden: bool = False
    allow_mining: bool = True
    mining_productivity: float = 0.0
    research_productivity: bool = True
    validate_stage: bool = True
    mode: Mode = 'targets'
    limits: Limits | None = None
    consume: dict[str, float] = {}
    disposal: DisposalMode = 'report'
    disposal_defaults: Defaults | None = None


class Fingerprint(BaseModel):
    prototype_raw_sha256: str | None
    progress_tick: int | None
    source_copy_sha256: str | None


class LineSummary(BaseModel):
    id: str
    recipe: str
    machine: str
    modules: list[str]
    machines: float
    machines_ceil: int
    beacon_count: float
    beacon_count_ceil: int


class BlockResult(BaseModel):
    """What plan_solve keeps of a block's last solve: enough to sum, compare and pin, not the full result."""

    solved_at: str
    fingerprint: Fingerprint
    status: PlanStatus | Literal['error']
    error: str | None = None
    solver: SolverName | None = None
    mode: Mode | None = None
    scale: float | None = None
    resolved_targets: dict[str, float] = Field({}, description='Target values used, references resolved')
    achieved_targets: dict[str, float] = {}
    totals: Totals | None = None
    disposal_totals: Totals | None = None
    imports: dict[str, float] = {}
    surplus: dict[str, float] = {}
    consume: dict[str, float] = {}
    module_inventory: dict[str, ModuleCount] = {}
    lines: list[LineSummary] = []
    lines_for_matrix: list[LineSpec] = []
    matrix_args: MatrixArgs | None = None
    warnings: list[str] = []

    @classmethod
    def from_plan(
        cls, r: PlanResult, resolved: dict[str, float], fingerprint: Fingerprint, solved_at: str
    ) -> 'BlockResult':
        return cls(
            solved_at=solved_at,
            fingerprint=fingerprint,
            status=r.status,
            solver=r.solver,
            mode=r.mode,
            scale=r.scale,
            resolved_targets=resolved,
            achieved_targets=r.achieved_targets,
            totals=r.totals,
            disposal_totals=r.disposal_totals,
            imports=r.imports,
            surplus=r.surplus,
            consume=r.consume,
            module_inventory=r.module_inventory,
            lines=[LineSummary.model_validate(line.model_dump()) for line in r.lines],
            lines_for_matrix=r.lines_for_matrix,
            matrix_args=r.matrix_args,
            warnings=r.warnings,
        )


class Block(Spec):
    id: str = Field(pattern=ID_PATTERN)
    description: str = ''
    enabled: bool = True
    request: BlockRequest
    result: BlockResult | None = None


class PlanFile(Spec):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)

    schema_version: Literal[1] = Field(1, alias='schema')
    name: str
    description: str = ''
    revision: int = 0
    created_at: str
    updated_at: str
    force: str = ''
    per: Per = 'second'
    fingerprint: Fingerprint
    blocks: list[Block] = []

    def block(self, block_id: str) -> Block:
        hit = next((b for b in self.blocks if b.id == block_id), None)
        if hit is None:
            raise ValueError(f'No block {block_id} in plan {self.name}; blocks: {[b.id for b in self.blocks]}')
        return hit


class PlanSummary(BaseModel):
    name: str
    description: str
    blocks: int
    updated_at: str
    revision: int
    stale: Staleness


class PlanView(BaseModel):
    plan: PlanFile
    stale: Staleness = Field(description='Plan fingerprint against the current prototypes and save snapshot')
    result_stale: dict[str, Staleness] = Field({}, description='Per block with a saved result')


class BlockInput(Spec):
    id: str = Field(pattern=ID_PATTERN)
    description: str = ''
    enabled: bool = True
    request: BlockRequest


class AddBlock(Spec):
    op: Literal['add_block']
    block: BlockInput


class RemoveBlock(Spec):
    op: Literal['remove_block']
    block_id: str


class RenameBlock(Spec):
    op: Literal['rename_block']
    block_id: str
    new_id: str = Field(pattern=ID_PATTERN)


class SetEnabled(Spec):
    op: Literal['set_enabled']
    block_id: str
    enabled: bool


class UpdateRequest(Spec):
    op: Literal['update_request']
    block_id: str
    fields: dict[str, Any] = Field(description='Shallow merge into the request; null removes a key')


class AddLine(Spec):
    op: Literal['add_line']
    block_id: str
    line: str | LineSpec


class UpdateLine(Spec):
    op: Literal['update_line']
    block_id: str
    line_id: str
    fields: dict[str, Any] = Field(description='Shallow merge into the line spec; null removes a key')


class RemoveLine(Spec):
    op: Literal['remove_line']
    block_id: str
    line_id: str


class SetTarget(Spec):
    op: Literal['set_target']
    block_id: str
    item: str
    value: TargetValue


class RemoveTarget(Spec):
    op: Literal['remove_target']
    block_id: str
    item: str


class SetLimit(Spec):
    op: Literal['set_limit']
    block_id: str
    key: str = Field(
        description='power_MW, machines, beacons, pollution_per_minute, or imports.<item>, '
        'machines_by_type.<machine>, beacons_by_type.<beacon>, modules.<module>'
    )
    value: float


class RemoveLimit(Spec):
    op: Literal['remove_limit']
    block_id: str
    key: str


class SetConsume(Spec):
    op: Literal['set_consume']
    block_id: str
    item: str
    value: float


class RemoveConsume(Spec):
    op: Literal['remove_consume']
    block_id: str
    item: str


class Pin(Spec):
    op: Literal['pin']
    block_id: str
    solver: SolverName | None = None


class SetMeta(Spec):
    op: Literal['set_meta']
    description: str | None = None
    force: str | None = None
    per: Per | None = None


class SetModules(Spec):
    op: Literal['set_modules']
    block_id: str
    line_id: str = Field(description='A line id, or "defaults" for the block default module priority list')
    modules: ModuleSpec | None = Field(description='null removes the setting (back to default filling)')


class SetBeacons(Spec):
    op: Literal['set_beacons']
    block_id: str
    line_id: str = Field(description='A line id, or "defaults" for the block default beacons')
    beacons: list[BeaconSpec] | None


class ReplaceModule(Spec):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)

    op: Literal['replace_module']
    from_: str = Field(alias='from')
    to: str
    block_id: str | None = Field(None, description='Omit for every block')


EditOp = Annotated[
    AddBlock
    | RemoveBlock
    | RenameBlock
    | SetEnabled
    | UpdateRequest
    | AddLine
    | UpdateLine
    | RemoveLine
    | SetTarget
    | RemoveTarget
    | SetLimit
    | RemoveLimit
    | SetConsume
    | RemoveConsume
    | Pin
    | SetMeta
    | SetModules
    | SetBeacons
    | ReplaceModule,
    Field(discriminator='op'),
]


class PlanDeleted(BaseModel):
    name: str
    trash_path: str


class EditResult(BaseModel):
    plan: str
    revision: int
    notes: list[str] = []
    warnings: list[str] = []
