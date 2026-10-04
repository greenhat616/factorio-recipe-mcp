"""Consistency of saved analysis outputs (scripts/analysis/methanol.py --force ...)."""
import json

import pytest

from recipe_mcp.paths import DATA_DIR


def test_methanol_current_stage_cases():
    path = DATA_DIR / 'methanol-current-stage.json'
    if not path.exists():
        pytest.skip('Run scripts/analysis/methanol.py --force <force> first')
    for case in json.loads(path.read_text(encoding='utf-8')):
        if case.get('feasible') is False:
            continue
        assert case['stage_validation']['valid_at_stage'] and case['max_balance_error'] < 1e-6
