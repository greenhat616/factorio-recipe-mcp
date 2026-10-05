"""The offline viewer embeds untrusted JSON without turning it into active HTML."""

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner
from test_plans import RAW

from recipe_mcp.database import Database
from recipe_mcp.names import Names
from recipe_mcp.planner import plan
from recipe_mcp.viewer import render
from recipe_mcp.viewer.__main__ import main


def test_viewer_escapes_payload_and_title() -> None:
    result = plan(Database(raw=RAW), {'b': 1}, lines=['ab'], graph='bipartite', validate_stage=False).model_dump(
        mode='json'
    )
    attack = '</script><img src=x onerror=alert(1)>'
    result['warnings'].append(attack)
    html = render(result, attack)
    assert attack not in html
    assert '&lt;/script&gt;' in html
    match = re.search(r'<script type="application/json" id="payload">(.*?)</script>', html, re.S)
    assert match is not None and json.loads(match[1]) == result
    assert not re.search(r'<(?:script|link)[^>]+(?:src|href)=["\']https?://', html)
    assert render(result, attack) == html


def test_viewer_cli_and_mcp_envelope(tmp_path: Path) -> None:
    result = plan(Database(raw=RAW), {'b': 1}, lines=['ab'], graph='bipartite', validate_stage=False).model_dump(
        mode='json'
    )
    envelope = {'content': [{'type': 'text', 'text': json.dumps(result)}]}
    assert render(envelope) == render(result)
    source, output = tmp_path / 'input.json', tmp_path / 'output.html'
    source.write_text(json.dumps(envelope))
    invocation = CliRunner().invoke(main, [str(source), str(output)])
    assert invocation.exit_code == 0, invocation.output
    assert output.read_text(encoding='utf-8') == render(result)
    with pytest.raises(ValueError, match='needs graph'):
        render({'status': 'optimal'})


def test_viewer_language_embedding_and_version_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = {**RAW, 'item': {'b': {}}}
    db = Database(raw=raw)
    db.raw_sha256 = 'current'
    db.names = Names(raw, {'en': {'item-name.b': 'Output'}, 'zh-CN': {'item-name.b': '产物'}})
    monkeypatch.setattr('recipe_mcp.viewer.__main__.Database', lambda: db)
    result = plan(db, {'b': 1}, lines=['ab'], graph='bipartite', validate_stage=False).model_dump(mode='json')
    source, output = tmp_path / 'input.json', tmp_path / 'output.html'
    source.write_text(json.dumps(result), encoding='utf-8')
    invocation = CliRunner().invoke(main, [str(source), str(output), '--language', 'zh-CN', '--language', 'en'])
    assert invocation.exit_code == 0, invocation.output
    match = re.search(r'id="payload">(.*?)</script>', output.read_text(encoding='utf-8'), re.S)
    assert match
    payload = json.loads(match[1])
    assert payload['names']['language'] == 'zh-CN'
    assert payload['names']['translations']['zh-CN']['item:b']['display_name'] == '产物'
    assert payload['lines'] == result['lines'] and payload['graph'] == result['graph']
    result['names']['raw_sha256'] = 'older'
    source.write_text(json.dumps(result), encoding='utf-8')
    invocation = CliRunner().invoke(main, [str(source), str(output), '--language', 'en'])
    assert invocation.exit_code != 0 and 'prototypes differ' in invocation.output
