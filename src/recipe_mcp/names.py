"""Resolve display names without changing the prototype IDs used by the solver."""

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

ITEM_TYPES = {
    'item',
    'ammo',
    'armor',
    'capsule',
    'gun',
    'module',
    'tool',
    'repair-tool',
    'item-with-entity-data',
    'item-with-inventory',
    'item-with-label',
    'item-with-tags',
    'blueprint',
    'blueprint-book',
    'copy-paste-tool',
    'deconstruction-item',
    'upgrade-item',
    'selection-tool',
    'spidertron-remote',
    'rail-planner',
    'space-platform-starter-pack',
}


class DisplayName(BaseModel):
    display_name: str = ''
    language: str = 'en'
    resolved_language: str | None = None
    name_status: Literal['translated', 'fallback', 'raw'] = 'raw'


class ObjectRef(BaseModel):
    kind: str
    name: str


class ObjectName(DisplayName):
    name: str
    kind: str
    localised_name: Any = None


class NameCatalog(BaseModel):
    raw_sha256: str | None = None
    language: str = 'en'
    available_languages: list[str] = []
    translations: dict[str, dict[str, ObjectName]] = {}
    warnings: list[str] = []


class Names:
    def __init__(
        self, raw: dict[str, Any], catalogs: dict[str, dict[str, str]] | None = None, warnings: list[str] | None = None
    ):
        self.raw_sha256: str | None = None
        self._cache: dict[tuple[str, str, str], ObjectName] = {}
        self.raw = raw
        self.catalogs = catalogs or {}
        self.warnings = warnings or []
        self.entity_types: set[str] = set()
        self.groups: dict[str, dict[str, Any]] = {'item': {}, 'entity': {}, 'equipment': {}}
        for kind, prototypes in raw.items():
            if kind in ITEM_TYPES:
                self.groups['item'].update(prototypes)
            elif kind.endswith('-equipment'):
                self.groups['equipment'].update(prototypes)
            elif kind not in ('recipe', 'technology', 'fluid') and (
                kind
                in {
                    'explosion',
                    'fire',
                    'smoke-with-trigger',
                    'particle-source',
                    'character-corpse',
                    'projectile',
                    'entity-ghost',
                }
                or any('collision_box' in p or 'crafting_speed' in p for p in prototypes.values())
            ):
                # Boxes are optional per entity, so classify the type before indexing its instances.
                self.entity_types.add(kind)
                self.groups['entity'].update(prototypes)

    @classmethod
    def from_snapshot(cls, raw: dict[str, Any], path: Path, raw_sha256: str | None) -> 'Names':
        if not raw_sha256:
            return cls(raw, warnings=['No prototype hash; locale snapshot cannot be matched.'])
        if not path.exists():
            return cls(raw, warnings=['Locale snapshot missing; run python -m recipe_mcp.export.locales.'])
        snapshot = json.loads(path.read_text(encoding='utf-8'))
        if not raw_sha256 or snapshot.get('raw_sha256') != raw_sha256:
            return cls(raw, warnings=['Locale snapshot does not match these prototypes; names use IDs.'])
        names = cls(raw, snapshot['catalogs'], snapshot.get('warnings', []))
        names.raw_sha256 = raw_sha256
        return names

    def prototype(self, kind: str, name: str) -> dict[str, Any] | None:
        return self.groups.get(kind, self.raw.get(kind, {})).get(name)

    def _expression(self, kind: str, name: str, seen: frozenset[str]) -> Any:
        key = f'{kind}:{name}'
        if key in seen:
            return ['missing.cyclic-name']
        p = self.prototype(kind, name)
        if p is None:
            return ['missing.unknown-prototype']
        if 'localised_name' in p:
            return p['localised_name']
        seen = seen | {key}
        if kind == 'recipe':
            results = p.get('results', [])
            results = list(results.values()) if isinstance(results, dict) else results
            main = p.get('main_product', results[0]['name'] if len(results) == 1 else '')
            for product in results:
                if product['name'] == main == name:
                    return self._expression(product.get('type', 'item'), main, seen)
        if kind in ITEM_TYPES and p.get('place_result'):
            return self._expression('entity', p['place_result'], seen)
        if kind in ITEM_TYPES and p.get('place_as_equipment_result'):
            return self._expression('equipment', p['place_as_equipment_result'], seen)
        if kind == 'technology' and (match := re.fullmatch(r'(.+)-(\d+)', name)):
            base = ['technology-name.' + match[1]]
            return base if p.get('max_level', 1) != 1 else ['', base, ' ', match[2]]
        group = 'item' if kind in ITEM_TYPES else 'entity' if kind in self.entity_types else kind
        if kind.endswith('-equipment'):
            group = 'equipment'
        return [f'{group}-name.{name}']

    def _resolve(self, expr: Any, language: str, depth: int = 0) -> tuple[str, set[str]] | None:
        if depth > 20:
            return None
        if isinstance(expr, (str, int, float)):
            return str(expr), set()
        if not isinstance(expr, list) or not expr:
            return None
        key, *args = expr
        if key == '?':
            return next((r for a in args if (r := self._resolve(a, language, depth + 1)) is not None), None)
        params = [self._resolve(a, language, depth + 1) for a in args]
        if any(p is None for p in params):
            return None
        values = [p[0] for p in params if p is not None]
        used = set().union(*(p[1] for p in params if p is not None))
        if key == '':
            return ''.join(values), used
        template = self.catalogs.get(language, {}).get(key)
        source = language
        if template is None:
            template, source = self.catalogs.get('en', {}).get(key), 'en'
        if template is None:
            return None
        if '__plural_for_parameter__' in template:
            return None
        used.add(source)
        failed = False

        def reference(match: re.Match[str]) -> str:
            nonlocal failed
            kind, name = match.group(1).lower(), match.group(2)
            locale_key = f'{kind}-name.{name}'
            expression = (
                [locale_key]
                if locale_key in self.catalogs.get(language, {}) or locale_key in self.catalogs.get('en', {})
                else self._expression(kind, name, frozenset())
            )
            result = self._resolve(expression, language, depth + 1)
            if result is None:
                failed = True
                return match[0]
            used.update(result[1])
            return result[0]

        # Substitute once, so a parameter's literal text is never interpreted as another placeholder.
        template = re.sub(
            r'__(\d+)__', lambda m: values[int(m[1]) - 1] if 0 < int(m[1]) <= len(values) else m[0], template
        )
        template = re.sub(r'__(ITEM|FLUID|ENTITY|RECIPE|TECHNOLOGY|EQUIPMENT)__([^\s]+?)__', reference, template)
        if failed or re.search(r'__(?:[A-Z0-9][A-Z0-9_-]*|plural_for_parameter)__', template):
            return None
        return template.replace(r'\n', '\n'), used

    def get(self, kind: str, name: str, language: str = 'en') -> ObjectName:
        if not re.fullmatch(r'[a-z]{2,3}(?:-[A-Za-z]{2,4})?', language):
            raise ValueError('Use a Factorio language code such as en, zh-CN or de')
        cache_key = kind, name, language
        if cache_key in self._cache:
            return self._cache[cache_key]
        p = self.prototype(kind, name)
        result = self._resolve(self._expression(kind, name, frozenset()), language) if p is not None else None
        text, used = result if result is not None else (name, set())
        value = ObjectName(
            name=name,
            kind=kind,
            display_name=text,
            language=language,
            resolved_language=('en' if 'en' in used and language != 'en' else language) if result else None,
            name_status=('fallback' if language != 'en' and 'en' in used else 'translated') if result else 'raw',
            localised_name=p.get('localised_name') if p else None,
        )

        self._cache[cache_key] = value
        return value

    def fields(self, kind: str, name: str, language: str) -> dict[str, Any]:
        return self.get(kind, name, language).model_dump(include=set(DisplayName.model_fields))

    def key_name(self, key: str, language: str) -> ObjectName:
        kind, _, name = key.partition(':')
        if kind == 'energy':
            names = {'electric': ('Electricity', '电力'), 'heat': ('Heat', '热能')}
            if name in names:
                return ObjectName(
                    name=name,
                    kind=kind,
                    display_name=names[name][language == 'zh-CN'],
                    language=language,
                    resolved_language=language if language in ('en', 'zh-CN') else 'en',
                    name_status='translated' if language in ('en', 'zh-CN') else 'fallback',
                )
        if key == 'recipe:temperature-match':
            return ObjectName(
                name=name,
                kind=kind,
                display_name='温度匹配' if language == 'zh-CN' else 'Temperature transfer',
                language=language,
                resolved_language=language if language in ('en', 'zh-CN') else 'en',
                name_status='translated' if language in ('en', 'zh-CN') else 'fallback',
            )
        if kind == 'recipe' and ':' in name:
            operation, _, target = name.partition(':')
            if operation in ('mining', 'heat', 'generate', 'solar', 'wind'):
                base, *suffix = target.split('@', 1)
                value = self.get('resource' if operation == 'mining' else 'entity', base, language)
                return value.model_copy(
                    update={
                        'kind': kind,
                        'name': name,
                        'display_name': f'{operation}: {value.display_name}'
                        + (' @' + suffix[0] + '°C' if suffix else ''),
                    }
                )
        if kind == 'fluid':
            base = re.split(r'[@\[~]', name, maxsplit=1)[0]
            value = self.get(kind, base, language)
            return value.model_copy(update={'name': name, 'display_name': value.display_name + name[len(base) :]})
        return self.get(kind, name, language)

    def catalog(self, data: dict[str, Any], languages: list[str]) -> NameCatalog:
        keys: set[str] = set()

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for field, item in value.items():
                    if field == 'names':
                        continue
                    if (
                        field in ('recipe', 'machine', 'fuel', 'beacon', 'entity', 'item')
                        and isinstance(item, str)
                        and item
                    ):
                        kind = (
                            'entity'
                            if field in ('machine', 'beacon', 'entity')
                            else 'item'
                            if field == 'fuel'
                            else field
                        )
                        keys.add(item if item.startswith(('item:', 'fluid:', 'energy:')) else kind + ':' + item)
                    if field == 'modules' and isinstance(item, list):
                        keys.update('item:' + x for x in item if isinstance(x, str))
                    visit(field)
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)
            elif isinstance(value, str) and re.fullmatch(r'(?:item|fluid|energy):[^:\s]+', value):
                keys.add(value)

        visit(data)
        languages = list(dict.fromkeys([*languages, 'en']))
        return NameCatalog(
            raw_sha256=self.raw_sha256,
            language=languages[0],
            available_languages=sorted(self.catalogs),
            translations={lang: {k: self.key_name(k, lang) for k in sorted(keys)} for lang in languages},
            warnings=self.warnings,
        )
