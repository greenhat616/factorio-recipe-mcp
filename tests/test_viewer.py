"""The offline viewer embeds untrusted JSON without turning it into active HTML."""

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner
from test_plans import RAW

from recipe_mcp.database import Database
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
