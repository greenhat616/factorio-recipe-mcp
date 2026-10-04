param([string]$ForceName = '')
# Foreground stdio: MCP client owns process lifetime and protocol pipes.
# Environment is managed by uv (pyproject.toml/uv.lock); uv run syncs .venv if stale.
if ($ForceName) { uv run --directory $PSScriptRoot recipe-mcp --force $ForceName }
else { uv run --directory $PSScriptRoot recipe-mcp }
exit $LASTEXITCODE
