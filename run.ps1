# Launch the forge dashboard: make sure uv and Bun exist, then hand over to
# the TUI runner, which installs dependencies, builds the UI, starts the API,
# and opens the browser. Arguments are forwarded (try -- --help).
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Install-Tool {
    param([string]$Name, [string]$Installer, [string]$BinDir)
    if (Get-Command $Name -ErrorAction SilentlyContinue) { return }
    Write-Host "Installing $Name..."
    Invoke-RestMethod $Installer | Invoke-Expression
    $env:Path = "$BinDir;$env:Path"
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name was installed but is not on PATH yet; open a new terminal and run this again."
    }
}

Install-Tool -Name "uv" -Installer "https://astral.sh/uv/install.ps1" -BinDir "$HOME\.local\bin"
Install-Tool -Name "bun" -Installer "https://bun.sh/install.ps1" -BinDir "$HOME\.bun\bin"

& uv run -m forge_dashboard.runner @args
$code = $LASTEXITCODE
if ($code -ne 0 -and [Environment]::UserInteractive -and -not [Console]::IsInputRedirected) {
    Read-Host "The runner exited with code $code. Press Enter to close"
}
exit $code
