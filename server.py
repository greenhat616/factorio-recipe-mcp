import argparse
from mcp.server.fastmcp import FastMCP
from database import Database

ap = argparse.ArgumentParser()
ap.add_argument('--stdio', action='store_true', help='Compatibility flag; stdio is the only transport')
ap.add_argument('--force', help='Default force for stage-aware queries')
args = ap.parse_args()
db = Database(default_force=args.force)
mcp = FastMCP('factorio-recipes')

@mcp.tool()
def search_recipes(query: str, limit: int = 30, include_virtual: bool = False, available_only: bool = False, force: str = '') -> list:
    """Search prototype IDs or localized-name keys. Virtual logistics recipes excluded by default."""
    selected = db.require_force(force) if available_only else force
    return [db.recipe(n,selected) for n in db.recipes if query.lower() in n.lower() and
            (include_virtual or not db.virtual(n)) and
            (not available_only or db.availability(n,selected)['usable_at_stage'] is True)][:max(1, min(limit, 200))]

@mcp.tool()
def get_recipe(name: str, force: str = '') -> dict:
    """Recipe plus save-snapshot research gate. Unknown means no compatible snapshot or no force selected."""
    return db.recipe(name,force)

@mcp.tool()
def related_recipes(material: str, direction: str = 'both', include_virtual: bool = False, available_only: bool = False, force: str = '') -> dict:
    """All recipes producing/consuming an item or fluid ID; retains byproducts and packaging."""
    if direction not in ('both','producers','consumers'): raise ValueError('Invalid direction')
    selected = db.require_force(force) if available_only else force
    out = {}
    for key, index in [('producers', db.producers), ('consumers', db.consumers)]:
        if direction in ('both', key):
            out[key] = [db.recipe(n,selected) for n in index.get(material, []) if (include_virtual or not db.virtual(n))
                        and (not available_only or db.availability(n,selected)['usable_at_stage'] is True)]
    return out

@mcp.tool()
def production_chain(material: str, depth: int = 2, max_recipes: int = 80, available_only: bool = True, force: str = '') -> dict:
    """Bounded upstream alternatives, cycle-safe. This is a graph, not an optimized production plan."""
    selected = db.require_force(force) if available_only else force
    seen, recipes, frontier = set(), {}, {material}
    truncated = False
    for _ in range(max(0, min(depth, 6))):
        next_layer = set()
        for item in sorted(frontier - seen):
            seen.add(item)
            for n in db.producers.get(item, []):
                if db.virtual(n) or n in recipes: continue
                if available_only and db.availability(n,selected)['usable_at_stage'] is not True: continue
                if len(recipes) >= max(1, min(max_recipes, 300)):
                    truncated = True
                    continue
                recipes[n] = db.recipe(n,selected)
                next_layer.update(e['name'] for e in db.recipes[n].get('ingredients', []))
        frontier = next_layer
    return {'recipes': list(recipes.values()), 'unexpanded_materials': sorted(frontier - seen), 'truncated': truncated}

@mcp.tool()
def compatible_machines(recipe: str, force: str = '', buildable_only: bool = False) -> list:
    """Category-compatible prototypes with speed, power, modules and fluid boxes; check fluid constraints."""
    selected = db.require_force(force) if buildable_only else force
    return [m for m in db.machines(recipe,selected) if not buildable_only or m['construction']['buildable_at_stage'] is True]

@mcp.tool()
def technology_requirements(technology: str, force: str = '') -> dict:
    """Science/trigger requirements, prerequisite tree, and selected force's actual technology state."""
    return db.science(technology,force)

@mcp.tool()
def get_progress_context() -> dict:
    """Snapshot save provenance, compatibility, tick, available forces, current research and queue."""
    return db.context()

@mcp.tool()
def list_technologies(query: str = '', state: str = 'all', force: str = '', include_hidden: bool = False, offset: int = 0, limit: int = 50) -> dict:
    """Paginated techs: researched, researching, available_to_research, locked, disabled, unknown, all."""
    if state not in ('all','researched','researching','available_to_research','locked','disabled','unknown'): raise ValueError('Invalid state')
    selected = db.require_force(force) if state not in ('all','unknown') else force
    rows = [db.technology(n,selected) for n,t in sorted(db.raw.get('technology',{}).items())
            if query.lower() in n.lower() and (include_hidden or not t.get('hidden',False))]
    if state != 'all': rows = [r for r in rows if r['state']==state]
    offset,limit = max(0,offset), max(1,min(limit,100))
    return {'total':len(rows),'offset':offset,'limit':limit,'technologies':rows[offset:offset+limit]}

@mcp.tool()
def get_technology(name: str, force: str = '') -> dict:
    """Technology unlock effects, prerequisites, cost/trigger, saved level/progress, and research gate."""
    return db.technology(name,force)

@mcp.tool()
def validate_plan(recipe_rates: dict[str,float], force: str = '', machines: list[str] = [], modules: list[str] = []) -> dict:
    """Check locked, disabled, virtual or unknown recipes for a force. Research gates only."""
    return db.validate_plan(recipe_rates,force,machines,modules)

@mcp.tool()
def net_balance(recipe_rates: dict[str, float], force: str = '', validate_stage: bool = True) -> dict:
    """Expected base rates; rejects locked recipes by default. validate_stage=false is theory mode. No productivity applied."""
    if validate_stage:
        validation = db.validate_plan(recipe_rates,force)
        if not validation['valid_at_stage']: raise ValueError('Stage-locked recipes: '+', '.join(validation['blocked_recipes']))
    return db.balance(recipe_rates)

if __name__ == '__main__':
    mcp.run(transport='stdio')
