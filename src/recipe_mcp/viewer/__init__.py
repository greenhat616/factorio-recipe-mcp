"""Standalone, offline HTML export for production and factory results."""

import json
from html import escape
from importlib.resources import files
from typing import Any


def unwrap(data: dict[str, Any]) -> dict[str, Any]:
    if data.get('structuredContent'):
        return unwrap(data['structuredContent'])
    if isinstance(data.get('content'), list):
        for content in data['content']:
            if content.get('type') == 'text':
                return unwrap(json.loads(content['text']))
    if not isinstance(data.get('graph'), dict) or data['graph'].get('schema_version') != 1:
        raise ValueError('Result needs graph schema 1: solve_production graph=bipartite or plan_solve graph=full')
    return data


def render(data: dict[str, Any], title: str = '生产规划观察器') -> str:
    data = unwrap(data)
    payload = json.dumps(data, ensure_ascii=False, allow_nan=False)
    # HTML's script end tag is recognised even inside a JSON string.
    payload = payload.replace('<', r'\u003c').replace('>', r'\u003e').replace('&', r'\u0026')
    template = files(__package__).joinpath('report.html').read_text(encoding='utf-8')
    return template.replace('__TITLE__', escape(title)).replace('__DATA__', payload)
