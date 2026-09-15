param([string]$ForceName = '')
# Foreground stdio: MCP client owns process lifetime and protocol pipes.
$recipePython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$recipeServer = Join-Path $PSScriptRoot 'server.py'
if ($ForceName) { & $recipePython $recipeServer --force $ForceName }
else { & $recipePython $recipeServer }
exit $LASTEXITCODE
