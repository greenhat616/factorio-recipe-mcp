"""Planner inputs (line specs, defaults), the normalised line model, solver solutions and results."""

from typing import Literal

from pydantic import BaseModel, Field

from ..models import JSON, Spec

# Either an explicit list (repeats allowed) or {module: count}.
ModuleSpec = list[str] | dict[str, int]
Per = Literal['second', 'minute', 'hour']
SolverName = Literal['lp', 'matrix']
Objective = Literal['balanced', 'machines', 'power', 'imports']
Mode = Literal['targets', 'maximize']
DisposalMode = Literal['report', 'none']


class BeaconSpec(Spec):
    beacon: str
    count: int = Field(1, ge=1, description='Beacons affecting each machine')
    modules: ModuleSpec = []
    per_machine: float | None = Field(
        None, ge=0, description='Beacons powered per machine when shared; defaults to count'
    )


class LineSpec(Spec):
    recipe: str = Field(description='Recipe name, or mining:<resource>')
    id: str | None = None
    machine: str | None = None
    modules: ModuleSpec | None = Field(None, description='Omit to fill slots from defaults.modules')
    beacons: list[BeaconSpec] | None = Field(None, description='Omit to use defaults.beacons')
    fixed_machines: float | None = Field(None, ge=0)
    max_machines: float | None = Field(None, ge=0)
    cost_weight: float = Field(1, ge=0)
    ignore_module_rules: bool = False


class Defaults(Spec):
    machine_preference: Literal['fastest', 'efficient', 'slowest'] = 'fastest'
    machines: list[str] = Field([], description='Preferred machine names, first compatible wins')
    modules: list[str] = Field([], description='Priority list; the first compatible module fills all slots')
    beacons: list[BeaconSpec] = []


class Limits(Spec):
    imports: dict[str, float] = Field({}, description='Import cap per item, in units per `per`')
    power_MW: float | None = Field(None, ge=0, description='Average electric power incl. drain and beacons')
    machines: float | None = Field(None, ge=0, description='Fractional production machines, excl. beacons and disposal')
    machines_by_type: dict[str, float] = Field({}, description='Fractional machine count cap per machine name')
    pollution_per_minute: float | None = Field(None, ge=0)


class LimitRow(BaseModel):
    """One limit in internal per-second units: a row over line crafts, or a cap on one item's import."""

    name: str
    limit: float
    coef: list[float] = Field([], description='Usage per craft/s of each line; empty for an import cap')
    item: str | None = Field(None, description='Item whose import this caps')
    rate: bool = Field(False, description='Limit and usage are rates that scale with `per`')
    unit: str


class ConstraintState(BaseModel):
    """A limit at the solution, internal units; marginal is the objective gain per unit of relaxation."""

    name: str
    limit: float
    used: float
    marginal: float | None
    rate: bool
    unit: str


class Infeasibility(BaseModel):
    """One relaxation the elastic LP needed: a limit raised, or a target or consume amount lowered."""

    kind: Literal['limit', 'target', 'consume']
    name: str = Field(description='Limit name, or the item of a target or consume amount')
    requested: float = Field(description='The limit, target or consume amount as given')
    achievable: float = Field(description='The limit needed (kind=limit), or the amount reached')
    shortfall: float
    relative: float | None = Field(description='shortfall / requested; null when requested is 0')
    rate: bool = Field(False, description='Amounts are rates that scale with `per`')
    unit: str


class Weights(BaseModel):
    """Objective weights per machine, per MW, per unit/s imported and per unit/s surplus."""

    machines: float
    power_MW: float
    imports: float
    surplus: float


WeightName = Literal['machines', 'power_MW', 'imports', 'surplus']


class RecipeEntry(BaseModel):
    type: str = 'item'
    name: str
    amount: float | None = None
    amount_min: float = 0
    amount_max: float = 0
    probability: float = 1
    ignored_by_productivity: float = 0
    extra_count_fraction: float = 0
    temperature: float | None = None
    minimum_temperature: float | None = None
    maximum_temperature: float | None = None

    @property
    def key(self) -> str:
        return f'{self.type}:{self.name}'

    @property
    def average(self) -> float:
        return self.amount if self.amount is not None else (self.amount_min + self.amount_max) / 2

    def output(self, productivity: float = 0.0) -> float:
        """Expected output per craft; productivity skips ignored_by_productivity."""
        avg = self.average
        ignored = min(avg, self.ignored_by_productivity)
        p = self.probability
        return p * avg + self.extra_count_fraction + p * max(0.0, avg - ignored) * productivity


class RecipeView(BaseModel):
    """A crafting recipe or a mining:<resource> pseudo-recipe in one shape."""

    name: str
    mining: bool
    category: str
    energy_required: float
    ingredients: list[RecipeEntry]
    results: list[RecipeEntry]
    allow_productivity: bool
    maximum_productivity: float
    allow: dict[str, bool]


class Machine(BaseModel):
    """A candidate machine: prototype kind, name and the raw prototype."""

    kind: str
    name: str
    proto: JSON


class TemperatureRange(BaseModel):
    temperature: float | None = None
    minimum_temperature: float | None = None
    maximum_temperature: float | None = None


class BeaconRow(BaseModel):
    beacon: str
    count: int
    modules: list[str]
    per_machine: float
    effect_factor: float


class Line(BaseModel):
    """One normalised production line: per-craft balance, rates, power and research gates."""

    id: str
    recipe: str
    machine: str
    machine_type: str
    modules: list[str]
    beacons: list[BeaconRow]
    speed_multiplier: float
    productivity: float
    consumption_multiplier: float
    pollution_multiplier: float
    research_productivity: float
    crafts_per_machine: float
    energy_type: str
    active_W: float
    drain_W: float
    beacon_W: float
    pollution_per_minute: float
    balance: dict[str, float]
    temperatures: dict[str, list[float]]
    ingredient_temperatures: dict[str, TemperatureRange]
    fixed_machines: float | None
    max_machines: float | None
    cost_weight: float
    blocked: list[str]

    @property
    def electric(self) -> bool:
        return self.energy_type == 'electric'

    @property
    def power_W(self) -> float:
        """Electric power per running machine; fuel-burning machines only draw for their beacons."""
        return (self.active_W + self.drain_W if self.electric else 0.0) + self.beacon_W

    def pin(self) -> LineSpec:
        """The spec that rebuilds exactly this line."""
        return LineSpec(
            id=self.id,
            recipe=self.recipe,
            machine=self.machine,
            modules=self.modules,
            beacons=[
                BeaconSpec(beacon=b.beacon, count=b.count, modules=b.modules, per_machine=b.per_machine)
                for b in self.beacons
            ],
        )


ItemRole = Literal['target', 'raw', 'byproduct', 'intermediate', 'consumed_input']


class MatrixInfo(BaseModel):
    items: int
    unknowns: int
    rank: int
    degrees_of_freedom: int
    roles: dict[str, ItemRole]
    unknown_columns: list[str]


class Solution(BaseModel):
    """Solver output in internal per-second units; x is crafts per second per line."""

    x: list[float]
    imports: dict[str, float]
    surplus: dict[str, float]
    residual: float
    objective: float | None = None
    prices: dict[str, float] | None = None
    status: str | None = None
    matrix: MatrixInfo | None = None
    negative: list[str] = []
    scale: float | None = None
    constraints: list[ConstraintState] = []
    warnings: list[str] = []
    infeasibility: list[Infeasibility] = []


class PlanLine(BaseModel):
    id: str
    recipe: str
    machine: str
    modules: list[str]
    beacons: list[BeaconRow]
    crafts: float
    machines: float
    machines_ceil: int
    speed_multiplier: float
    productivity: float
    consumption_multiplier: float
    power_MW: float
    installed_power_MW: float
    energy_type: str
    fuel_MW: float = Field(description='Fuel or heat consumed by non-electric machines')
    pollution_per_minute: float
    inputs: dict[str, float]
    outputs: dict[str, float]


class ItemFlow(BaseModel):
    item: str
    produced: float = 0.0
    consumed: float = 0.0
    imported: float = 0.0
    supplied: float = Field(0.0, description='External supply that must be used up (consume)')
    surplus: float = 0.0
    target: float = 0.0


class Totals(BaseModel):
    machines: float = 0.0
    machines_ceil: int = 0
    power_MW: float = 0.0
    installed_power_MW: float = 0.0
    non_electric_fuel_MW: float = 0.0
    pollution_per_minute: float = 0.0


class Report(BaseModel):
    lines: list[PlanLine]
    items: list[ItemFlow]
    imports: dict[str, float]
    surplus: dict[str, float]
    totals: Totals
    max_balance_error: float
    warnings: list[str]
    shadow_prices: dict[str, float] | None = Field(
        None, description='Marginal objective cost of one more unit per `per` of each item (lp only)'
    )
    objective: float | None = None
    matrix: MatrixInfo | None = None


class LimitUsage(BaseModel):
    constraint: str
    limit: float
    used: float
    utilization: float | None = Field(description='used / limit; null when the limit is 0')
    unit: str


class Bottleneck(BaseModel):
    constraint: str
    limit: float
    used: float
    marginal: float
    meaning: str


class ExcludedCandidate(BaseModel):
    recipe: str
    reason: str


class MatrixArgs(BaseModel):
    surplus_items: list[str]
    imports: list[str]


class SnapshotProvenance(BaseModel):
    tick: int | None
    source_save: str | None
    source_copy_sha256: str | None
    exported_at: str | None


PlanStatus = Literal['optimal', 'infeasible', 'time_limit']


class PlanResult(Report):
    status: PlanStatus = 'optimal'
    solver: SolverName
    mode: Mode = 'targets'
    per: Per
    force: str | None
    validate_stage: bool
    targets: dict[str, float] = Field(description='Requested rates; ratios when mode=maximize')
    scale: float | None = Field(None, description='mode=maximize: achieved rate = scale x ratio, per `per`')
    achieved_targets: dict[str, float] = Field({}, description='Target rates actually produced, per `per`')
    consume: dict[str, float] = Field({}, description='External supplies used up exactly, per `per`')
    limits_usage: list[LimitUsage] = []
    bottlenecks: list[Bottleneck] = Field(
        [], description='Binding limits; marginal = scale (maximize) or objective (targets) gain per +1 limit unit'
    )
    bottlenecks_note: str | None = None
    infeasibility: list[Infeasibility] = Field(
        [], description='status=infeasible: relaxations that make the plan feasible; lines show the relaxed plan'
    )
    suggestions: list[str] = []
    note: str | None = None
    disposal: list[PlanLine] = Field([], description='Void machines for each surplus byproduct (disposal="report")')
    disposal_totals: Totals | None = None
    totals_with_disposal: Totals | None = None
    disposal_unhandled: list[str] = Field([], description='Surplus items with no usable void recipe')
    candidate_lines: int
    excluded_candidates: list[ExcludedCandidate]
    lines_for_matrix: list[LineSpec] = Field(description='Pass back as `lines` to pin this plan')
    matrix_args: MatrixArgs | None = Field(None, description='Pass with lines_for_matrix to the matrix solver')
    scope: str = (
        'Expected-value rates. Research gates checked when validate_stage; quality, surface effects, '
        'logistics throughput and fluid-resource depletion not modelled.'
    )
    snapshot_provenance: SnapshotProvenance | None = None


class MachinePower(BaseModel):
    active: float
    drain: float
    beacons: float


class RateRequirement(BaseModel):
    item: str
    rate: float
    machines: float
    machines_ceil: int
    power_MW: float


class MachineStats(BaseModel):
    recipe: str
    machine: str
    machine_type: str
    modules: list[str]
    beacons: list[BeaconRow]
    speed_multiplier: float
    productivity: float
    research_productivity: float
    consumption_multiplier: float
    pollution_multiplier: float
    energy_type: str
    blocked: list[str]
    per: Per
    crafts_per_machine: float
    per_machine: dict[str, float]
    power_per_machine_MW: MachinePower
    pollution_per_minute_per_machine: float
    valid_at_stage: bool | None
    alternatives: list[str]
    warnings: list[str]
    for_rate: RateRequirement | None = None


class ProductionMatrix(BaseModel):
    lines: list[str]
    items: list[str]
    rank_recipe_matrix: int
    square_system: MatrixInfo
    blocked: dict[str, list[str]]
    hint: str | None = None
    matrix: list[list[float]] | None = Field(None, description='Dense items x lines, for small systems')
    nonzeros: list[tuple[str, str, float]] | None = Field(None, description='(item, line, value) for large systems')
