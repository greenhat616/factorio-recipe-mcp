"""Shared fixtures. Real-data tests skip (not fail) when data/ has no compatible export."""
import os

import pytest

from recipe_mcp.database import Database
from recipe_mcp.paths import RAW_DUMP

DEFAULT_FORCE = 'faction-a632079'


@pytest.fixture(scope='session')
def real_db() -> Database:
    if not RAW_DUMP.exists():
        pytest.skip('No prototype dump: run `uv run recipe-mcp-export`')
    db = Database()
    if not db.progress_compatible:
        pytest.skip('No compatible save snapshot: run `uv run recipe-mcp-export-save`')
    return db


@pytest.fixture(scope='session')
def force(real_db: Database) -> str:
    name = os.environ.get('RECIPE_MCP_TEST_FORCE', DEFAULT_FORCE)
    if name not in real_db.progress.get('forces', {}):
        pytest.skip(f'Force {name} not in snapshot; set RECIPE_MCP_TEST_FORCE')
    return name
