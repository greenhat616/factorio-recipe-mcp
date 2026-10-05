"""MCP stdio server: recipe/technology/progress queries and production planning tools."""

from pathlib import Path
from typing import Literal

import click
from mcp.server.fastmcp import FastMCP

from .database import Database
from .models import (
    MachineInfo,
    PlanValidation,
    ProductionChain,
    ProgressContext,
    RecipeInfo,
    RelatedRecipes,
    ScienceRequirements,
    TechnologyInfo,
    TechnologyPage,
)
from .names import NameCatalog, ObjectRef
from .paths import PLANS_DIR
from .planner import machine_stats as planner_machine_stats
from .planner import plan
from .planner import production_matrix as planner_matrix
from .planner.schema import (
    BeaconSpec,
    Defaults,
    DisposalMode,
    EnergyMode,
    GraphOptions,
    Limits,
    LineSpec,
    MachineStats,
    Mode,
    ModuleSpec,
    Objective,
    Per,
    PlanResult,
    ProductionMatrix,
    SolverName,
    WeightName,
)
from .plans import PlanStore
from .plans.factory import Comparison, Detail, FactoryResult, compare_plans, solve_plan
from .plans.models import BlockInput, EditOp, EditResult, PlanDeleted, PlanSummary, PlanView

StateFilter = Literal['all', 'researched', 'researching', 'available_to_research', 'locked', 'disabled', 'unknown']


def create_server(db: Database, plans_dir: Path = PLANS_DIR) -> FastMCP:
    """Register every tool against one prototype/progress database and one plan folder."""
    mcp = FastMCP('factorio-recipes')
    store = PlanStore(plans_dir, db)

    @mcp.tool()
    def get_object_names(objects: list[ObjectRef], language: str = 'en') -> NameCatalog:
        """Resolve up to 200 object names. kind: recipe/item/fluid/entity/technology or an exact prototype type.
        IDs stay unchanged; name_status and resolved_language expose English/ID fallbacks."""
        if len(objects) > 200:
            raise ValueError('At most 200 objects per request')
        return NameCatalog(
            raw_sha256=db.raw_sha256,
            language=language,
            available_languages=sorted(db.names.catalogs),
            translations={language: {f'{o.kind}:{o.name}': db.names.get(o.kind, o.name, language) for o in objects}},
            warnings=db.names.warnings,
        )

    @mcp.tool()
    def search_recipes(
        query: str,
        limit: int = 30,
        include_virtual: bool = False,
        available_only: bool = False,
        force: str = '',
        language: str = 'en',
    ) -> list[RecipeInfo]:
        """Search prototype IDs or translated display names (English by default). Virtual logistics recipes excluded by default."""
        selected = db.require_force(force) if available_only else force
        return [
            db.recipe(n, selected, language)
            for n in db.recipes
            if (
                query.casefold() in n.casefold()
                or query.casefold() in db.names.get('recipe', n, language).display_name.casefold()
            )
            and (include_virtual or not db.virtual(n))
            and (not available_only or db.availability(n, selected).usable_at_stage is True)
        ][: max(1, min(limit, 200))]

    @mcp.tool()
    def get_recipe(name: str, force: str = '', language: str = 'en') -> RecipeInfo:
        """Recipe plus save-snapshot research gate. Unknown means no compatible snapshot or no force selected."""
        return db.recipe(name, force, language)

    @mcp.tool()
    def related_recipes(
        material: str,
        direction: Literal['both', 'producers', 'consumers'] = 'both',
        include_virtual: bool = False,
        available_only: bool = False,
        force: str = '',
        language: str = 'en',
    ) -> RelatedRecipes:
        """All recipes producing/consuming an item or fluid ID; retains byproducts and packaging."""
        selected = db.require_force(force) if available_only else force

        def recipes(index: dict[str, list[str]]) -> list[RecipeInfo]:
            return [
                db.recipe(n, selected, language)
                for n in index.get(material, [])
                if (include_virtual or not db.virtual(n))
                and (not available_only or db.availability(n, selected).usable_at_stage is True)
            ]

        return RelatedRecipes(
            producers=recipes(db.producers) if direction in ('both', 'producers') else None,
            consumers=recipes(db.consumers) if direction in ('both', 'consumers') else None,
        )

    @mcp.tool()
    def production_chain(
        material: str,
        depth: int = 2,
        max_recipes: int = 80,
        available_only: bool = True,
        force: str = '',
        language: str = 'en',
    ) -> ProductionChain:
        """Bounded upstream alternatives, cycle-safe. This is a graph, not an optimized production plan."""
        selected = db.require_force(force) if available_only else force
        seen: set[str] = set()
        recipes: dict[str, RecipeInfo] = {}
        frontier = {material}
        truncated = False
        for _ in range(max(0, min(depth, 6))):
            next_layer: set[str] = set()
            for item in sorted(frontier - seen):
                seen.add(item)
                for n in db.producers.get(item, []):
                    if db.virtual(n) or n in recipes:
                        continue
                    if available_only and db.availability(n, selected).usable_at_stage is not True:
                        continue
                    if len(recipes) >= max(1, min(max_recipes, 300)):
                        truncated = True
                        continue
                    recipes[n] = db.recipe(n, selected, language)
                    next_layer.update(e['name'] for e in recipes[n].ingredients)
            frontier = next_layer
        return ProductionChain(
            recipes=list(recipes.values()), unexpanded_materials=sorted(frontier - seen), truncated=truncated
        )

    @mcp.tool()
    def compatible_machines(
        recipe: str, force: str = '', buildable_only: bool = False, language: str = 'en'
    ) -> list[MachineInfo]:
        """Category-compatible prototypes with speed, power, modules and fluid boxes; check fluid constraints."""
        selected = db.require_force(force) if buildable_only else force
        return [
            m
            for m in db.machines(recipe, selected, language)
            if not buildable_only or m.construction.buildable_at_stage is True
        ]

    @mcp.tool()
    def technology_requirements(technology: str, force: str = '', language: str = 'en') -> ScienceRequirements:
        """Science/trigger requirements, prerequisite tree, and selected force's actual technology state."""
        return db.science(technology, force, language)

    @mcp.tool()
    def get_progress_context() -> ProgressContext:
        """Snapshot save provenance, compatibility, tick, available forces, current research and queue."""
        return db.context()

    @mcp.tool()
    def list_technologies(
        query: str = '',
        state: StateFilter = 'all',
        force: str = '',
        include_hidden: bool = False,
        offset: int = 0,
        limit: int = 50,
        language: str = 'en',
    ) -> TechnologyPage:
        """Paginated technologies, optionally filtered by name and research state."""
        selected = db.require_force(force) if state not in ('all', 'unknown') else force
        rows = [
            db.technology(n, selected, language)
            for n, t in sorted(db.raw.get('technology', {}).items())
            if (
                query.casefold() in n.casefold()
                or query.casefold() in db.names.get('technology', n, language).display_name.casefold()
            )
            and (include_hidden or not t.get('hidden', False))
        ]
        if state != 'all':
            rows = [r for r in rows if r.state == state]
        offset, limit = max(0, offset), max(1, min(limit, 100))
        return TechnologyPage(total=len(rows), offset=offset, limit=limit, technologies=rows[offset : offset + limit])

    @mcp.tool()
    def get_technology(name: str, force: str = '', language: str = 'en') -> TechnologyInfo:
        """Technology unlock effects, prerequisites, cost/trigger, saved level/progress, and research gate."""
        return db.technology(name, force, language)

    @mcp.tool()
    def validate_plan(
        recipe_rates: dict[str, float], force: str = '', machines: list[str] = [], modules: list[str] = []
    ) -> PlanValidation:
        """Check locked, disabled, virtual or unknown recipes for a force. Research gates only."""
        return db.validate_plan(recipe_rates, force, machines, modules)

    @mcp.tool()
    def net_balance(recipe_rates: dict[str, float], force: str = '', validate_stage: bool = True) -> dict[str, float]:
        """Expected base rates; rejects locked recipes by default. validate_stage=false is theory mode. No productivity applied."""
        if validate_stage:
            validation = db.validate_plan(recipe_rates, force)
            if not validation.valid_at_stage:
                raise ValueError('Stage-locked recipes: ' + ', '.join(validation.blocked_recipes))
        return db.balance(recipe_rates)

    @mcp.tool()
    def solve_production(
        targets: dict[str, float],
        lines: list[str | LineSpec] = [],
        solver: SolverName = 'lp',
        per: Per = 'second',
        defaults: Defaults | None = None,
        auto_discover: bool | None = None,
        imports: list[str] = [],
        forbid_imports: list[str] = [],
        import_costs: dict[str, float] = {},
        allow_surplus: bool = True,
        surplus_items: list[str] = [],
        objective: Objective = 'balanced',
        weights: dict[WeightName, float] = {},
        exclude_recipes: list[str] = [],
        max_depth: int = 12,
        max_lines: int = 1000,
        include_hidden: bool = False,
        allow_mining: bool = True,
        mining_productivity: float = 0.0,
        research_productivity: bool = True,
        force: str = '',
        validate_stage: bool = True,
        mode: Mode = 'targets',
        limits: Limits | None = None,
        consume: dict[str, float] = {},
        disposal: DisposalMode = 'report',
        disposal_defaults: Defaults | None = None,
        integer_machines: bool = False,
        time_limit: float = 10.0,
        copies: int = 1,
        graph: Literal['none', 'bipartite'] = 'none',
        graph_options: GraphOptions | None = None,
        energy_mode: EnergyMode = 'report',
        solar_factor: float = 0.7,
        wind_factor: float | None = None,
        language: str = 'en',
    ) -> PlanResult:
        """Helmod/Factory Planner style rate calculator: machine counts, modules/beacons, power, imports, byproducts.

        targets: {material: rate per `per`}; material is an ID or item:/fluid: prefixed ID.
        lines: recipe names or line specs; a recipe may be "mining:<resource>".
        solver='lp' (default): HiGHS LP picks among alternatives, auto-discovers upstream recipes when lines is empty,
                imports only raw items (no producing line) or `imports`, penalises surplus, minimises objective
                balanced (default: machines + 0.01/unit import + 0.1/unit surplus) | machines | power | imports;
                weights override single objective weights; import_costs scales per item; returns shadow_prices.
        solver='matrix': exact square-system solve over the given lines (one recipe per intermediate); reports
                underdetermined null space or inconsistent items; negative unknowns are warned, not hidden.
        mode='maximize' (lp only): targets are ratios; returns the largest `scale` (output = scale x ratio per `per`)
                that fits `limits`, then the cheapest plan at that scale.
        limits (lp only): {imports: {item: rate per `per`}, power_MW, machines, machines_by_type: {machine: count},
                beacons, beacons_by_type: {beacon: count}, modules: {module: count}, pollution_per_minute}.
                machines excludes beacons; beacons count entities by per_machine share; modules count machine slots
                plus beacon slots. Capped imports are allowed even when the item has a producing line.
                Returns limits_usage for every limit and bottlenecks for binding ones with their marginal value.
                Several lines of one recipe with different modules (distinct ids) let limits.modules allocate them.
        Beacons: count = beacons affecting each machine (effect), per_machine = beacon entities per machine (power,
                counts; less than count when shared). interference=k uses the Nullius variant <beacon>-k.
                Results give beacon_count / beacon_count_ceil / beacon_power_MW per line and in totals,
                totals.beacon_count_by_type and module_inventory.
        consume: {material: rate per `per`} supplied externally and used up exactly (Helmod input mode); never
                imported or left as surplus. With the matrix solver targets may be empty: outputs become byproducts.
        disposal='report' (default): void machines (chimneys, outfalls) for each surplus byproduct, chosen with
                disposal_defaults (machine_preference defaults to efficient); reported in disposal, disposal_totals
                and totals_with_disposal (totals itself excludes them); disposal_unhandled lists items with no void.
                Never affects route choice. disposal='none' skips it.
        energy_mode="balance": balance electricity/heat (MW regardless of per) and selected fuels.
        Energy sources: heat:<reactor>, generate:<generator>, solar:<panel>, wind:<interface>.
        Set line.fuel or defaults.fuel for burners; wind recipes require wind_factor.
        copies divides construction into identical copies; rates and limits remain block totals.
        integer_machines (lp only, <= 400 lines): whole machine counts carry the machine cost and the machine, power,
                pollution, beacon and module limits (MILP, time_limit seconds per stage; status time_limit with mip_gap
                if stopped early). No marginal values and no elastic diagnosis. Pin routes first for large plans.
        Stage-locked recipes/machines/modules are rejected unless validate_stage=false.
        Feed result.lines_for_matrix back as `lines` (with result.matrix_args for the matrix solver) to pin a plan."""
        return plan(
            db,
            targets,
            lines,
            solver=solver,
            per=per,
            defaults=defaults,
            auto_discover=auto_discover,
            imports=imports,
            forbid_imports=forbid_imports,
            import_costs=import_costs,
            allow_surplus=allow_surplus,
            surplus_items=surplus_items,
            objective=objective,
            weights=weights,
            exclude_recipes=exclude_recipes,
            max_depth=max_depth,
            max_lines=max_lines,
            include_hidden=include_hidden,
            allow_mining=allow_mining,
            mining_productivity=mining_productivity,
            research_productivity=research_productivity,
            force=force,
            validate_stage=validate_stage,
            mode=mode,
            limits=limits,
            consume=consume,
            disposal=disposal,
            disposal_defaults=disposal_defaults,
            integer_machines=integer_machines,
            time_limit=time_limit,
            copies=copies,
            graph=graph,
            graph_options=graph_options,
            energy_mode=energy_mode,
            solar_factor=solar_factor,
            wind_factor=wind_factor,
            language=language,
        )

    @mcp.tool()
    def machine_stats(
        recipe: str,
        machine: str = '',
        modules: ModuleSpec | None = None,
        beacons: list[BeaconSpec] | None = None,
        defaults: Defaults | None = None,
        rate: float | None = None,
        item: str = '',
        per: Per = 'second',
        mining_productivity: float = 0.0,
        research_productivity: bool = True,
        force: str = '',
        validate_stage: bool = True,
        energy_mode: EnergyMode = 'report',
        fuel: str | None = None,
        neighbours: int = 0,
        solar_factor: float = 0.7,
        wind_factor: float | None = None,
        temperature: float | None = None,
    ) -> MachineStats:
        """One recipe in one machine: effective speed/productivity/consumption, I/O per machine, power, pollution.
        With rate (and optional item, default main output) returns machines needed. recipe may be "mining:<resource>".
        beacons[].interference=k uses the Nullius interference variant <beacon>-k; beacon_items and module_counts
        give beacon entities and modules per machine (shared beacons by per_machine)."""
        return planner_machine_stats(
            db,
            recipe,
            machine,
            modules,
            beacons,
            defaults,
            rate,
            item,
            per,
            force,
            validate_stage,
            mining_productivity,
            research_productivity,
            energy_mode,
            fuel,
            neighbours,
            solar_factor,
            wind_factor,
            temperature,
        )

    @mcp.tool()
    def production_matrix(
        lines: list[str | LineSpec],
        targets: dict[str, float] = {},
        imports: list[str] = [],
        surplus_items: list[str] = [],
        defaults: Defaults | None = None,
        force: str = '',
        validate_stage: bool = False,
        consume: dict[str, float] = {},
    ) -> ProductionMatrix:
        """Stoichiometric matrix (items x lines, net per craft incl. productivity), ranks, item roles
        (target/raw/byproduct/intermediate/consumed_input) and degrees of freedom of the matrix solver's square system."""
        return planner_matrix(
            db, lines, targets, imports, surplus_items, defaults, force, validate_stage, consume=consume
        )

    @mcp.tool()
    def plan_list() -> list[PlanSummary]:
        """Saved plans: name, description, block count, revision, update time and staleness against current data."""
        return store.list()

    @mcp.tool()
    def plan_get(name: str, include_results: bool = True) -> PlanView:
        """A saved plan with every block request and (unless include_results=false) each block's last result summary.
        stale: fresh | stage_changed (save snapshot moved on) | prototypes_changed (mods changed) | unknown."""
        return store.view(name, include_results)

    @mcp.tool()
    def plan_save(
        name: str,
        blocks: list[BlockInput],
        description: str = '',
        force: str = '',
        per: Per = 'second',
        overwrite: bool = False,
    ) -> PlanView:
        """Create a plan (or replace one with overwrite=true). name: 1-64 of A-Z a-z 0-9 _ . -.
        Each block: {id, description?, enabled?, request}; request takes solve_production arguments except force and
        per, which are plan-wide. A target may be a link {"from": [block ids] | "*", "plus": n}: the sum of what those
        blocks import of that item. Blocks are checked like solve_production (without solving) before writing."""
        return store.save(name, blocks, description, force, per, overwrite)

    @mcp.tool()
    def plan_edit(name: str, ops: list[EditOp], expected_revision: int | None = None) -> EditResult:
        """Apply edit operations atomically (all or nothing) and bump the revision; a different expected_revision is a
        conflict. ops: add_block, remove_block, rename_block (links follow), set_enabled, update_request (shallow merge,
        null deletes), add_line / update_line / remove_line (by line id = id or recipe), set_target / remove_target,
        set_limit / remove_limit (key power_MW, machines, beacons, pollution_per_minute, imports.<item>,
        machines_by_type.<machine>, beacons_by_type.<beacon>, modules.<module>), set_consume / remove_consume,
        pin (lines := last result's lines_for_matrix, optional solver), set_meta (description, force, per),
        set_modules / set_beacons (a line id or "defaults"; null removes), replace_module (from, to, block_id?)."""
        return store.edit(name, ops, expected_revision)

    @mcp.tool()
    def plan_solve(
        name: str,
        blocks: list[str] = [],
        detail: Detail = 'summary',
        save_results: bool = True,
        graph: Literal['none', 'blocks', 'full'] = 'none',
        graph_options: GraphOptions | None = None,
        language: str = 'en',
    ) -> FactoryResult:
        """Solve enabled blocks (or the listed ones plus the blocks they link to) in dependency order and sum them:
        net_inputs / net_outputs / internal_transfers per item on a shared-bus assumption, totals, beacons, modules,
        and disposal for surplus no other block takes. A failing block is reported and the rest still solve.
        detail="full" adds each block's full solve_production result. save_results writes the summaries back."""
        return solve_plan(store, name, blocks, detail, save_results, graph, graph_options, language)

    @mcp.tool()
    def plan_compare(a: str, b: str) -> Comparison:
        """Compare saved results of two plans ("plan": its enabled blocks summed) or blocks ("plan/block"): totals,
        imports and surplus side by side with diff = b - a. Results must exist and not be stale: run plan_solve first."""
        return compare_plans(store, a, b)

    @mcp.tool()
    def plan_delete(name: str, confirm: str) -> PlanDeleted:
        """Move a plan to the trash folder (data/plans/.trash); confirm must equal the plan name."""
        return PlanDeleted(name=name, trash_path=store.delete(name, confirm))

    return mcp


@click.command(help=__doc__)
@click.option('--stdio', is_flag=True, help='Compatibility flag; stdio is the only transport.')
@click.option('--force', help='Default force for stage-aware queries.')
def main(stdio: bool, force: str | None) -> None:
    create_server(Database(default_force=force)).run(transport='stdio')


if __name__ == '__main__':
    main()
