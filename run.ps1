# Check for uv and Bun, then launch the TUI runner. Put --install-tools first
# to opt into downloading missing tools. Runner arguments follow (try --help).
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$installTools = $args[0] -ceq "--install-tools"
if ($installTools) {
    $args = @($args | Select-Object -Skip 1)
}

function Ensure-Tool {
    param([string]$Name, [string]$Installer, [string]$BinDir)
    if (Get-Command $Name -ErrorAction SilentlyContinue) { return }
    if (-not $installTools) {
        [Console]::Error.WriteLine("$Name is required but missing from PATH. Install it with your package manager.")
        [Console]::Error.WriteLine("On Arch, add the $Name package to your arcus module.")
        [Console]::Error.WriteLine("To use the upstream installer, run ./run.ps1 --install-tools.")
        exit 1
    }

    Write-Host "Installing $Name..."
    Invoke-RestMethod $Installer | Invoke-Expression
    $env:PATH = (Join-Path $HOME $BinDir) + [IO.Path]::PathSeparator + $env:PATH
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name is still missing from PATH after running its installer."
    }
}

Ensure-Tool -Name "uv" -Installer "https://astral.sh/uv/install.ps1" -BinDir ".local/bin"
Ensure-Tool -Name "bun" -Installer "https://bun.sh/install.ps1" -BinDir ".bun/bin"

& uv run -m forge_dashboard.runner @args
$code = $LASTEXITCODE
if ($code -ne 0 -and [Environment]::UserInteractive -and -not [Console]::IsInputRedirected) {
    Read-Host "The runner exited with code $code. Press Enter to close"
}
exit $code
