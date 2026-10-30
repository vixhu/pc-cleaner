Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
if (Get-Command py -ErrorAction SilentlyContinue) {
    py -3 "$PSScriptRoot\main.py"
} else {
    python "$PSScriptRoot\main.py"
}
