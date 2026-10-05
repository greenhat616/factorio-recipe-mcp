"""Planner inputs (line specs, defaults), the normalised line model, solver solutions and results."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..models import JSON, Spec
from ..names import NameCatalog

# Either an explicit list (repeats allowed) or {module: count}.
ModuleSpec = list[str] | dict[str, int]
Per = Literal['second', 'minute', 'hour']
SolverName = Literal['lp', 'matrix']
Objective = Literal['balanced', 'machines', 'power', 'imports']
Mode = Literal['targets', 'maximize']
EnergyMode = Literal['report', 'balance']
ProductivityModel = Literal['expected', 'helmod']
DisposalMode = Literal['report', 'none']


class BeaconSpec(Spec):
    beacon: str
    count: int = Field(1, ge=1, description='Beacons affecting each machine')
    modules: ModuleSpec = []
    per_machine: float | None = Field(
        None, ge=0, description='Beacons powered per machine when shared; defaults to count'
    )
    interference: int = Field(
        0,
        ge=0,
        le=4,
        description='Nullius small beacon covered by k large-beacon interference fields: uses variant <beacon>-<k>',
    )


class LineSpec(Spec):
    productivity_model: ProductivityModel | None = None
    recipe: str = Field(description='Recipe name, or mining:<resource>')
    id: str | None = None
    machine: str | None = None
    modules: ModuleSpec | None = Field(None, description='Omit to fill slots from defaults.modules')
    beacons: list[BeaconSpec] | None = Field(None, description='Omit to use defaults.beacons')
    fixed_machines: float | None = Field(None, ge=0)
    max_machines: float | None = Field(None, ge=0)
    cost_weight: float = Field(1, ge=0)
    ignore_module_rules: bool = False
    fuel: str | None = None
    neighbours: int = Field(0, ge=0)
    temperature: float | None = Field(None, allow_inf_nan=False)


class Defaults(Spec):
    productivity_model: ProductivityModel = 'expected'
    fuel: list[str] = []
    machine_preference: Literal['fastest', 'efficient', 'slowest'] = 'fastest'
    machines: list[str] = Field([], description='Preferred machine names, first compatible wins')
    modules: list[str] = Field([], description='Priority list; the first compatible module fills all slots')
    beacons: list[BeaconSpec] = []
    module_options: list[JSON] | None = Field(
        None, description='Reserved: one candidate line per module configuration. Not implemented yet.'
    )


class Limits(Spec):
    imports: dict[str, float] = Field({}, description='Import cap per item, in units per `per`')
    power_MW: float | None = Field(None, ge=0, description='Average electric power incl. drain and beacons')
    machines: float | None = Field(None, ge=0, description='Fractional production machines, excl. beacons and disposal')
    machines_by_type: dict[str, float] = Field({}, description='Fractional machine count cap per machine name')
    pollution_per_minute: float | None = Field(None, ge=0)
    beacons: float | None = Field(None, ge=0, description='Fractional beacon entities (per_machine share)')
    beacons_by_type: dict[str, float] = Field({}, description='Beacon cap per beacon entity or item name')
    modules: dict[str, float] = Field({}, description='Module cap per module name, machine and beacon slots')


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
    # Reserved: cost per beacon and per module. Not implemented yet; must stay 0.
    beacons: float = 0.0
    modules: float = 0.0


WeightName = Literal['machines', 'power_MW', 'imports', 'surplus', 'beacons', 'modules']


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

    def output(self, productivity: float = 0.0, model: ProductivityModel = 'expected') -> float:
        """Expected output per craft; productivity skips ignored_by_productivity."""
        avg = self.average
        ignored = min(avg, self.ignored_by_productivity)
        p = self.probability
        bonus = max(0.0, p * avg - self.ignored_by_productivity) if model == 'helmod' else p * max(0.0, avg - ignored)
        return p * avg + self.extra_count_fraction + bonus * productivity


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
    beacon: str = Field(description='As given in the spec')
    entity: str = Field(description='Beacon prototype used, after resolving interference')
    item: str = Field(description='Item that places the beacon; variants count as their base beacon')
    interference: int
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
    electric_consumption_W: float = 0.0
    productivity_model: ProductivityModel = 'expected'
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
    fuel: str | None = None
    fuel_per_machine: float = 0.0
    neighbours: int = 0
    temperature: float | None = None
    beacon_items: dict[str, float] = Field({}, description='Beacon entities per machine, by beacon item')
    module_counts: dict[str, float] = Field({}, description='Modules per machine: own slots + shared beacon slots')

    @property
    def beacon_entities(self) -> float:
        return sum(self.beacon_items.values())

    @property
    def electric(self) -> bool:
        return self.energy_type == 'electric'

    @property
    def power_W(self) -> float:
        """Electric power per running machine; fuel-burning machines only draw for their beacons."""
        return (self.active_W + self.drain_W if self.electric else 0.0) + self.beacon_W + self.electric_consumption_W

    def pin(self) -> LineSpec:
        """The spec that rebuilds exactly this line."""
        return LineSpec(
            id=self.id,
            recipe=self.recipe,
            machine=self.machine,
            fuel=self.fuel,
            productivity_model=self.productivity_model,
            neighbours=self.neighbours,
            temperature=self.temperature,
            modules=self.modules,
            beacons=[
                BeaconSpec(
                    beacon=b.beacon,
                    count=b.count,
                    modules=b.modules,
                    per_machine=b.per_machine,
                    interference=b.interference,
                )
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
    counts: list[int] | None = Field(None, description='Whole machines per line (integer mode)')
    mip_gap: float | None = None
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
    productivity_model: ProductivityModel = 'expected'
    machine_type: str = ''
    fuel: str | None = None
    fuel_per_machine: float = 0.0
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
    beacon_count: float = Field(0.0, description='machines x beacons per machine (per_machine share)')
    beacon_count_ceil: int = Field(0, description='ceil(machines_ceil x beacons per machine)')
    beacon_power_MW: float = 0.0


class ItemFlow(BaseModel):
    unit: str = ''
    item: str
    produced: float = 0.0
    consumed: float = 0.0
    imported: float = 0.0
    supplied: float = Field(0.0, description='External supply that must be used up (consume)')
    surplus: float = 0.0
    target: float = 0.0


class Totals(BaseModel):
    electric_generation_MW: float = 0.0
    heat_generation_MW: float = 0.0
    heat_consumption_MW: float = 0.0
    net_electric_MW: float = 0.0
    machines: float = 0.0
    machines_ceil: int = 0
    power_MW: float = 0.0
    installed_power_MW: float = 0.0
    non_electric_fuel_MW: float = 0.0
    pollution_per_minute: float = 0.0
    beacon_count: float = 0.0
    beacon_count_ceil: int = 0
    beacon_count_by_type: dict[str, float] = Field({}, description='Fractional beacons by beacon item')
    beacon_power_MW: float = Field(0.0, description='Part of power_MW drawn by beacons')


class ModuleCount(BaseModel):
    count: float = Field(description='Fractional: machines x modules per machine')
    count_ceil: int = Field(description='From rounded-up machines and beacons')


class PerCopy(BaseModel):
    lines: list[PlanLine]
    totals: Totals
    module_inventory: dict[str, ModuleCount]


class Report(BaseModel):
    temperatures: dict[str, list[ItemFlow]] = {}
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


class GraphOptions(Spec):
    allocate: bool = False
    format: Literal['mermaid', 'dot'] | None = None


class GraphNode(BaseModel):
    id: str
    kind: str
    label: str
    depth: int | None = None
    data: dict[str, Any] = {}


class GraphEdge(BaseModel):
    source: str
    target: str
    item: str
    rate: float
    unit: str
    kind: Literal['flow', 'allocated', 'link'] = 'flow'
    allocated: bool = False
    data: dict[str, Any] = {}


class ProductionGraph(BaseModel):
    schema_version: Literal[1] = 1
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    notes: list[str] = []


class PlanResult(Report):
    names: NameCatalog | None = None
    graph: ProductionGraph | None = Field(None, exclude_if=lambda v: v is None)
    energy_mode: EnergyMode = 'report'
    energy_factors: dict[str, float | None] = {}
    copies: int = 1
    per_copy: PerCopy | None = None
    copies_totals: Totals | None = None
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
    integer_machines: bool = False
    mip_gap: float | None = Field(None, description='Relative MIP gap; > 0 when the time limit stopped the search')
    infeasibility: list[Infeasibility] = Field(
        [], description='status=infeasible: relaxations that make the plan feasible; lines show the relaxed plan'
    )
    suggestions: list[str] = []
    note: str | None = None
    disposal: list[PlanLine] = Field([], description='Void machines for each surplus byproduct (disposal="report")')
    disposal_totals: Totals | None = None
    totals_with_disposal: Totals | None = None
    disposal_unhandled: list[str] = Field([], description='Surplus items with no usable void recipe')
    module_inventory: dict[str, ModuleCount] = Field(
        {}, description='Modules in machine and beacon slots of all lines and disposal machines'
    )
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
    fuel: str | None = None
    fuel_per_machine: float = 0.0
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
    beacon_items: dict[str, float] = Field(description='Beacon entities per machine, by beacon item')
    module_counts: dict[str, float] = Field(description='Modules per machine incl. its share of beacon modules')
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
