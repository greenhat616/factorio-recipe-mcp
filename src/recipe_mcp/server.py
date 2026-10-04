"""MCP stdio server: recipe/technology/progress queries and production planning tools."""

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
from .planner import machine_stats as planner_machine_stats
from .planner import plan
from .planner import production_matrix as planner_matrix
from .planner.schema import (
    BeaconSpec,
    Defaults,
    LineSpec,
    MachineStats,
    ModuleSpec,
    Objective,
    Per,
    PlanResult,
    ProductionMatrix,
    SolverName,
    WeightName,
)

StateFilter = Literal['all', 'researched', 'researching', 'available_to_research', 'locked', 'disabled', 'unknown']


def create_server(db: Database) -> FastMCP:
    """Register every tool against one prototype/progress database."""
    mcp = FastMCP('factorio-recipes')

    @mcp.tool()
    def search_recipes(
        query: str, limit: int = 30, include_virtual: bool = False, available_only: bool = False, force: str = ''
    ) -> list[RecipeInfo]:
        """Search prototype IDs or localized-name keys. Virtual logistics recipes excluded by default."""
        selected = db.require_force(force) if available_only else force
        return [
            db.recipe(n, selected)
            for n in db.recipes
            if query.lower() in n.lower()
            and (include_virtual or not db.virtual(n))
            and (not available_only or db.availability(n, selected).usable_at_stage is True)
        ][: max(1, min(limit, 200))]

    @mcp.tool()
    def get_recipe(name: str, force: str = '') -> RecipeInfo:
        """Recipe plus save-snapshot research gate. Unknown means no compatible snapshot or no force selected."""
        return db.recipe(name, force)

    @mcp.tool()
    def related_recipes(
        material: str,
        direction: Literal['both', 'producers', 'consumers'] = 'both',
        include_virtual: bool = False,
        available_only: bool = False,
        force: str = '',
    ) -> RelatedRecipes:
        """All recipes producing/consuming an item or fluid ID; retains byproducts and packaging."""
        selected = db.require_force(force) if available_only else force

        def recipes(index: dict[str, list[str]]) -> list[RecipeInfo]:
            return [
                db.recipe(n, selected)
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
        material: str, depth: int = 2, max_recipes: int = 80, available_only: bool = True, force: str = ''
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
                    recipes[n] = db.recipe(n, selected)
                    next_layer.update(e['name'] for e in recipes[n].ingredients)
            frontier = next_layer
        return ProductionChain(
            recipes=list(recipes.values()), unexpanded_materials=sorted(frontier - seen), truncated=truncated
        )

    @mcp.tool()
    def compatible_machines(recipe: str, force: str = '', buildable_only: bool = False) -> list[MachineInfo]:
        """Category-compatible prototypes with speed, power, modules and fluid boxes; check fluid constraints."""
        selected = db.require_force(force) if buildable_only else force
        return [
            m for m in db.machines(recipe, selected) if not buildable_only or m.construction.buildable_at_stage is True
        ]

    @mcp.tool()
    def technology_requirements(technology: str, force: str = '') -> ScienceRequirements:
        """Science/trigger requirements, prerequisite tree, and selected force's actual technology state."""
        return db.science(technology, force)

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
    ) -> TechnologyPage:
        """Paginated technologies, optionally filtered by name and research state."""
        selected = db.require_force(force) if state not in ('all', 'unknown') else force
        rows = [
            db.technology(n, selected)
            for n, t in sorted(db.raw.get('technology', {}).items())
            if query.lower() in n.lower() and (include_hidden or not t.get('hidden', False))
        ]
        if state != 'all':
            rows = [r for r in rows if r.state == state]
        offset, limit = max(0, offset), max(1, min(limit, 100))
        return TechnologyPage(total=len(rows), offset=offset, limit=limit, technologies=rows[offset : offset + limit])

    @mcp.tool()
    def get_technology(name: str, force: str = '') -> TechnologyInfo:
        """Technology unlock effects, prerequisites, cost/trigger, saved level/progress, and research gate."""
        return db.technology(name, force)

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
        Stage-locked recipes/machines/modules are rejected unless validate_stage=false.
        Feed result.lines_for_matrix back as `lines` (with result.matrix_args for the matrix solver) to pin a plan."""
        return plan(
            db,
            targets,
            lines,
            solver,
            per,
            defaults,
            auto_discover,
            imports,
            forbid_imports,
            import_costs,
            allow_surplus,
            surplus_items,
            objective,
            weights,
            exclude_recipes,
            max_depth,
            max_lines,
            include_hidden,
            allow_mining,
            mining_productivity,
            research_productivity,
            force,
            validate_stage,
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
    ) -> MachineStats:
        """One recipe in one machine: effective speed/productivity/consumption, I/O per machine, power, pollution.
        With rate (and optional item, default main output) returns machines needed. recipe may be "mining:<resource>"."""
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
    ) -> ProductionMatrix:
        """Stoichiometric matrix (items x lines, net per craft incl. productivity), ranks, item roles
        (target/raw/byproduct/intermediate) and degrees of freedom of the matrix solver's square system."""
        return planner_matrix(db, lines, targets, imports, surplus_items, defaults, force, validate_stage)

    return mcp


@click.command(help=__doc__)
@click.option('--stdio', is_flag=True, help='Compatibility flag; stdio is the only transport.')
@click.option('--force', help='Default force for stage-aware queries.')
def main(stdio: bool, force: str | None) -> None:
    create_server(Database(default_force=force)).run(transport='stdio')


if __name__ == '__main__':
    main()
