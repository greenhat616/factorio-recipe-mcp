"""Build the self-contained, offline pressure planning dashboard from solved data."""
import json
import re
from pathlib import Path
from recipe_mcp.paths import DATA_DIR, PROGRESS, RAW_DUMP, WORKSPACE_ROOT
from zones import build_zones

TEMPLATES = Path(__file__).resolve().parent / 'templates'


def build():
    scenarios = json.loads((DATA_DIR / 'pressure-transition-all.json').read_text(encoding='utf-8'))
    sections = {}
    locale_paths = list(Path('D:/Program Files (x86)/Steam/steamapps/common/Factorio/data/base/locale/zh-CN').glob('*.cfg'))
    locale_paths.append(WORKSPACE_ROOT / 'aotixnullius-hotfix/locale/zh-CN/nullius_2.0.10.cfg')
    for path in locale_paths:
        section = ''
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if line.startswith('['):
                section = line.strip('[]')
            elif '=' in line and not line.startswith(';'):
                key, value = line.split('=', 1)
                sections.setdefault(section, {})[key] = value
    names = {k: sections.get(k, {}) for k in ['fluid-name', 'item-name', 'recipe-name', 'technology-name', 'entity-name']}
    names['fluid-name']['nullius-water'] = '纯水'
    ratios = {}
    for s in scenarios:
        for _, (compressed, ratio) in s['gas_equivalence'].items():
            ratios[compressed] = ratio
    # Verified against final prototype compression/decompression recipes.
    ratios.update({'nullius-compressed-helium': 2.5, 'nullius-compressed-trace-gas': 4})
    keys = ['name', 'target_each_pack_per_second', 'future_projection', 'new_technologies',
            'volcanic_extraction_cap', 'fluid_bus', 'supplies', 'active_recipes',
            'machines_continuous', 'electrical_MW', 'thermal_MW', 'added_targets']
    raw = json.loads(RAW_DUMP.read_text(encoding='utf-8'))
    def localized(spec, depth=0):
        if depth > 12:
            return None
        if isinstance(spec, str):
            return spec
        if not isinstance(spec, list) or not spec or not isinstance(spec[0], str):
            return None
        if not spec[0]:
            values = [localized(v, depth + 1) for v in spec[1:]]
            return ''.join(values) if all(v is not None for v in values) else None
        section, _, key = spec[0].partition('.')
        value = sections.get(section, {}).get(key)
        if value is None:
            return None
        for i, arg in enumerate(spec[1:], 1):
            replacement = localized(arg, depth + 1)
            if replacement is None:
                return None
            value = value.replace(f'__{i}__', replacement)
        def expand(match):
            kind, ident = match.groups()
            return sections.get(kind.lower() + '-name', {}).get(ident, ident)
        return re.sub(r'__(ITEM|FLUID|ENTITY|RECIPE|TECHNOLOGY)__([^_]+(?:_[^_]+)*)__', expand, value)

    for kind in ['item', 'tool', 'ammo', 'capsule', 'module', 'fluid', 'recipe', 'assembling-machine', 'furnace', 'fluid-wagon']:
        section = 'fluid-name' if kind == 'fluid' else 'recipe-name' if kind == 'recipe' else 'entity-name' if kind in ['assembling-machine', 'furnace', 'fluid-wagon'] else 'item-name'
        for ident, proto in raw.get(kind, {}).items():
            translated = localized(proto.get('localised_name'))
            if translated:
                names[section][ident] = translated
    zoning = build_zones(scenarios, raw)
    (DATA_DIR / 'pressure-zones.json').write_text(json.dumps(zoning, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    progress = json.loads(PROGRESS.read_text(encoding='utf-8'))
    researched = {k for k, v in progress['forces']['faction-a632079']['technologies'].items() if v['researched']}
    rail = []
    for i, tech in enumerate(['nullius-freight-logistics', 'nullius-freight-transportation-2', 'nullius-freight-transportation-3'], 1):
        rail.append({'name': f'nullius-fluid-wagon-{i}', 'capacity': raw['fluid-wagon'][f'nullius-fluid-wagon-{i}']['capacity'],
                     'technology': tech, 'currently_available': tech in researched})
    payload = {'scenarios': [{k: s[k] for k in keys} for s in scenarios], 'names': names,
               'rail': rail, 'zoning': zoning,
               'ratios': ratios, 'provenance': scenarios[0]['provenance']}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c')
    template = (TEMPLATES / 'pressure-report.html').read_text(encoding='utf-8')
    out = WORKSPACE_ROOT / 'docs/nullius-pressure-stage-plan.html'
    template = template.replace('__RAIL_PANEL__', (TEMPLATES / 'pressure-rail-panel.html').read_text(encoding='utf-8'))
    template = template.replace('__RAIL_SCRIPT__', (TEMPLATES / 'pressure-rail.js').read_text(encoding='utf-8'))
    template = template.replace('__ZONES_PANEL__', (TEMPLATES / 'pressure-zones-panel.html').read_text(encoding='utf-8'))
    template = template.replace('__ZONES_SCRIPT__', (TEMPLATES / 'pressure-zones.js').read_text(encoding='utf-8'))
    out.write_text(template.replace('__REPORT_DATA__', encoded), encoding='utf-8')
    print(f'Built {out} ({out.stat().st_size:,} bytes), {len(scenarios)} scenarios')


if __name__ == '__main__':
    build()
