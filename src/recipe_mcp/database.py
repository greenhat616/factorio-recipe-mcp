"""Read-only indexes of final Factorio data-stage prototypes."""
import json
import hashlib
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .paths import PROGRESS, RAW_DUMP

# Prototypes, snapshots and tool results are JSON-shaped; their keys follow the game's data format.
JSON = dict[str, Any]

def amount(entry: JSON) -> float:
    return entry.get('probability', 1) * entry.get('amount', (entry.get('amount_min', 0) + entry.get('amount_max', 0)) / 2)

class Database:
    def __init__(self, path: str | Path | None = None, raw: JSON | None = None, progress: JSON | None = None,
                 default_force: str | None = None) -> None:
        if raw is None:
            blob = Path(path or RAW_DUMP).read_bytes()
            self.raw: JSON = json.loads(blob)
            self.raw_sha256: str | None = hashlib.sha256(blob).hexdigest()
        else:
            self.raw, self.raw_sha256 = raw, None
        self.default_force = default_force
        snapshot = PROGRESS
        self.progress: JSON = progress if progress is not None else (json.loads(snapshot.read_text(encoding='utf-8')) if snapshot.exists() else {})
        provenance = self.progress.get('provenance',{})
        self.progress_compatible = bool(self.raw_sha256 and provenance.get('prototype_raw_sha256')==self.raw_sha256
            and provenance.get('recipe_definitions_match') and provenance.get('data_stage_checksums_match'))
        self.recipes: dict[str, JSON] = self.raw['recipe']
        self.producers: defaultdict[str, list[str]] = defaultdict(list)
        self.consumers: defaultdict[str, list[str]] = defaultdict(list)
        self.unlocks: defaultdict[str, list[str]] = defaultdict(list)
        for name, recipe in self.recipes.items():
            for key, index in [('ingredients', self.consumers), ('results', self.producers)]:
                for entry in recipe.get(key, []):
                    index[entry['name']].append(name)
        for name, tech in self.raw.get('technology', {}).items():
            for effect in tech.get('effects', []):
                if effect['type'] == 'unlock-recipe':
                    self.unlocks[effect['recipe']].append(name)

    def _recipe(self, name: str) -> JSON:
        if name not in self.recipes: raise ValueError(f'Unknown recipe: {name}')
        return self.recipes[name]

    def _technology(self, name: str) -> JSON:
        technologies = self.raw.get('technology', {})
        if name not in technologies: raise ValueError(f'Unknown technology: {name}')
        return technologies[name]

    def recipe(self, name: str, force: str | None = None) -> JSON:
        r = self._recipe(name)
        keys = ['name', 'category', 'ingredients', 'results', 'allow_productivity', 'maximum_productivity',
                'hidden', 'localised_name', 'surface_conditions', 'allow_quality']
        result = {k: r[k] for k in keys if k in r}
        result.update(energy_required=r.get('energy_required', .5), category=r.get('category', 'crafting'),
                      enabled_at_start=r.get('enabled', True), unlock_technologies=self.unlocks[name],
                      virtual=self.virtual(name), availability=self.availability(name,force))
        return result

    def force_name(self, force: str | None = None) -> str | None:
        name = force or self.default_force
        if name:
            if name not in self.progress.get('forces',{}): raise ValueError(f'Unknown force: {name}')
            return name
        candidates = [n for n in self.progress.get('forces',{}) if n not in ['enemy','neutral']]
        return candidates[0] if len(candidates)==1 else None

    def require_force(self, force: str | None = None) -> str:
        if not self.progress_compatible: raise ValueError('A compatible save progress snapshot is required. Run recipe-mcp-export-save.')
        name = self.force_name(force)
        if not name: raise ValueError('Multiple forces: select a force explicitly with force or --force. See get_progress_context.')
        return name

    def context(self) -> JSON:
        return {'snapshot_loaded':bool(self.progress),'compatible':self.progress_compatible,
                'default_force':self.default_force,'tick':self.progress.get('tick'),'provenance':self.progress.get('provenance'),
                'forces':[{ 'name':n,'researched_count':sum(t['researched'] for t in f['technologies'].values()),
                           'enabled_recipe_count':sum(r['enabled'] for r in f['recipes'].values()),
                           'current_research':f.get('current_research'),'research_progress':f.get('research_progress'),
                           'research_queue':f.get('research_queue',[])} for n,f in self.progress.get('forces',{}).items()]}

    def availability(self, name: str, force: str | None = None) -> JSON:
        r = self._recipe(name)
        if self.virtual(name): return {'state':'virtual','usable_at_stage':False,'scope':'research gate only'}
        if not self.progress_compatible:
            return {'state':'unknown','usable_at_stage':None,'reason':'No compatible save snapshot', 'scope':'research gate only'}
        selected = self.force_name(force)
        if not selected:
            return {'state':'unknown','usable_at_stage':None,'reason':'Select a force', 'scope':'research gate only'}
        f = self.progress['forces'][selected]
        runtime = f['recipes'].get(name)
        if runtime is None: return {'state':'unknown','usable_at_stage':None,'reason':'Recipe missing in force snapshot'}
        researched_unlocks = [t for t in self.unlocks[name] if f['technologies'].get(t,{}).get('researched')]
        state = 'unlocked' if runtime['enabled'] else ('script_disabled' if researched_unlocks or r.get('enabled',True) else 'locked')
        return {'state':state,'usable_at_stage':runtime['enabled'],'enabled_in_save':runtime['enabled'],
                'force':selected,'researched_unlocks':researched_unlocks,
                'locked_unlock_alternatives':[t for t in self.unlocks[name] if t not in researched_unlocks],
                'productivity_bonus':runtime.get('productivity_bonus',0),
                'scope':'research gate only; machines, ingredients and surface conditions are separate'}

    def technology(self, name: str, force: str | None = None) -> JSON:
        t = self._technology(name)
        selected = self.force_name(force) if self.progress_compatible else None
        f = self.progress['forces'][selected] if selected else {}
        runtime = f.get('technologies',{}).get(name)
        prerequisites = runtime.get('prerequisites',[]) if runtime else t.get('prerequisites',[])
        missing = [n for n in prerequisites if not f.get('technologies',{}).get(n,{}).get('researched')] if runtime else None
        state = 'unknown'
        if runtime:
            state = ('researched' if runtime['researched'] else 'disabled' if not runtime['enabled'] or not f.get('research_enabled',True)
                     else 'researching' if f.get('current_research')==name else 'locked' if missing else 'available_to_research')
        keys = ['localised_name','unit','research_trigger','effects','hidden','max_level','upgrade']
        return {'name':name,**{k:t[k] for k in keys if k in t},'prerequisites':prerequisites,
                'unlocks_recipes':[e['recipe'] for e in t.get('effects',[]) if e['type']=='unlock-recipe'],
                'enabled_at_start':t.get('enabled',True), 'force':selected,'state':state,
                'runtime':runtime,'missing_prerequisites':missing,
                'progress':f.get('research_progress',0) if f.get('current_research')==name else runtime.get('saved_progress',0) if runtime else None,
                'note':'available_to_research checks prerequisite/enabled flags, not science supply or trigger completion'}

    def item_stage(self, name: str, force: str | None = None) -> JSON:
        # Packaging/unpackaging is not proof that a new machine/module can be manufactured.
        candidates = [n for n in self.producers.get(name,[]) if not self.virtual(n) and
                      not any(s in n for s in ['unbox','boxed','barrel','canister','recycling','legacy'])]
        candidates = [n for n in candidates if sum(amount(e) for e in self.recipes[n].get('results',[]) if e['name']==name)
                      > sum(amount(e) for e in self.recipes[n].get('ingredients',[]) if e['name']==name)]
        states = [self.availability(n,force) for n in candidates]
        usable = True if any(s['usable_at_stage'] is True for s in states) else False if states and all(s['usable_at_stage'] is False for s in states) else None
        return {'item':name,'buildable_at_stage':usable,'manufacturing_recipes':candidates,
                'scope':'manufacturing research gate only; existing inventory not inspected'}

    def machine_stage(self, name: str, force: str | None = None) -> JSON:
        m = next((self.raw[k][name] for k in ['assembling-machine','furnace','rocket-silo','mining-drill','beacon'] if name in self.raw.get(k,{})),None)
        if m is None: return {'machine':name,'buildable_at_stage':None,'reason':'No supported machine prototype'}
        place = m.get('placeable_by',{})
        item = place.get('item') if isinstance(place,dict) else None
        item = item or m.get('minable',{}).get('result')
        if not item:
            item = next((n for k in ['item','item-with-entity-data'] for n,i in self.raw.get(k,{}).items() if i.get('place_result')==name),None)
        return {'machine':name,**self.item_stage(item,force)} if item else {'machine':name,'buildable_at_stage':None,'reason':'No construction item mapping'}

    def validate_plan(self, rates: Mapping[str, float], force: str | None = None,
                      machines: Iterable[str] = (), modules: Iterable[str] = ()) -> JSON:
        if any(not math.isfinite(x) or x<0 for x in rates.values()): raise ValueError('Recipe rates must be finite and nonnegative')
        selected = self.require_force(force)
        rows = [{'recipe':n,**self.availability(n,selected)} if n in self.recipes else
                {'recipe':n,'state':'missing','usable_at_stage':False} for n,x in rates.items() if x>0]
        equipment = [self.machine_stage(n,selected) for n in machines]+[self.item_stage(n,selected) for n in modules]
        return {'force':selected,'valid_at_stage':all(r['usable_at_stage'] is True for r in rows) and all(e['buildable_at_stage'] is True for e in equipment),
                'recipes':rows,'blocked_recipes':[r['recipe'] for r in rows if r['usable_at_stage'] is not True],
                'equipment':equipment,'blocked_equipment':[e for e in equipment if e['buildable_at_stage'] is not True],
                'scope':'research gates only; does not prove ingredient supply, compatible machines or surface conditions'}

    def virtual(self, name: str) -> bool:
        r = self._recipe(name)
        return r.get('category', '').startswith(('transport-drone-', 'transport-fluid-', 'transport-item-')) or name.startswith(('creative-mod', 'nullius-creative', 'request-'))

    def machines(self, recipe: str, force: str | None = None) -> list[JSON]:
        category = self._recipe(recipe).get('category', 'crafting')
        result = []
        for kind in ['assembling-machine', 'furnace', 'rocket-silo', 'character']:
            for name, m in self.raw.get(kind, {}).items():
                if category in m.get('crafting_categories', []):
                    keys = ['crafting_speed', 'energy_usage', 'energy_source', 'module_slots', 'allowed_effects',
                            'effect_receiver', 'fluid_boxes', 'fixed_recipe']
                    result.append({'name': name, 'type': kind, **{k: m[k] for k in keys if k in m},
                                   'construction':self.machine_stage(name,force)})
        return result

    def science(self, technology: str, force: str | None = None) -> JSON:
        seen: set[str] = set()
        packs: set[str] = set()
        def visit(n: str) -> None:
            if n in seen: return
            seen.add(n)
            t = self._technology(n)
            for i in t.get('unit', {}).get('ingredients', []): packs.add(i[0] if isinstance(i, list) else i['name'])
            for p in t.get('prerequisites', []): visit(p)
        visit(technology)
        return {'technology': technology, 'science_packs': sorted(p for p in packs if p.endswith('-pack')),
                'requirement_tokens': sorted(p for p in packs if not p.endswith('-pack')),
                'prerequisites_transitive': sorted(seen - {technology}),
                'technology_state':self.technology(technology,force)}

    def balance(self, rates: Mapping[str, float]) -> dict[str, float]:
        if any(not math.isfinite(x) or x<0 for x in rates.values()): raise ValueError('Recipe rates must be finite and nonnegative')
        result: defaultdict[str, float] = defaultdict(float)
        for name, rate in rates.items():
            for key, sign in [('ingredients', -1), ('results', 1)]:
                for e in self._recipe(name).get(key, []): result[e['type'] + ':' + e['name']] += sign * amount(e) * rate
        return {n: v for n, v in sorted(result.items()) if abs(v) > 1e-9}
