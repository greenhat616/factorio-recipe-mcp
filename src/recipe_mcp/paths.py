"""Filesystem locations shared by the server, exporters and scripts.

The project is installed editable by ``uv sync``, so the project root is two
levels above this package. ``RECIPE_MCP_HOME`` overrides it (e.g. a different
data checkout); ``RECIPE_MCP_DATA`` overrides only the data directory.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get('RECIPE_MCP_HOME') or Path(__file__).resolve().parents[2])
DATA_DIR = Path(os.environ.get('RECIPE_MCP_DATA') or PROJECT_ROOT / 'data')
RAW_DUMP = DATA_DIR / 'script-output' / 'data-raw-dump.json'
PROGRESS = DATA_DIR / 'progress.json'
# Saved production plans; data/ is git-ignored, so plans stay local like the exports.
PLANS_DIR = DATA_DIR / 'plans'
# The hotfix workspace that holds this project, helper mods and planning docs.
WORKSPACE_ROOT = PROJECT_ROOT.parent
# Exporter defaults: the Steam install and the user data mods directory.
FACTORIO_EXE = Path(r'D:\Program Files (x86)\Steam\steamapps\common\Factorio\bin\x64\factorio.exe')
FACTORIO_MODS = Path.home() / 'AppData/Roaming/Factorio/mods'
