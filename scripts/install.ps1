# Bootstrap with Python; works in Windows PowerShell 5.1 and PowerShell 7.
param(
    [Parameter(Mandatory = $true)][string]$Url,
    [switch]$AddToPath,
    [string]$Prefix,
    [string]$BinDir
)
$ErrorActionPreference = 'Stop'

$pythonCommand = $null
$pythonArguments = @()
if ($env:PYTHON) {
    $candidates = @(@{ Command = $env:PYTHON; Arguments = @() })
} else {
    $candidates = @(
        @{ Command = 'py'; Arguments = @('-3') },
        @{ Command = 'python'; Arguments = @() },
        @{ Command = 'python3'; Arguments = @() }
    )
}
foreach ($candidate in $candidates) {
    if (Get-Command $candidate.Command -ErrorAction SilentlyContinue) {
        $probeArguments = @($candidate.Arguments) + @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)')
        & $candidate.Command @probeArguments
        if ($LASTEXITCODE -eq 0) {
            $pythonCommand = $candidate.Command
            $pythonArguments = @($candidate.Arguments)
            break
        }
    }
}
if (-not $pythonCommand) { throw 'MiniAgent requires Python 3.10 or newer. Install Python and retry.' }

$bootstrap = @'
import sys
from urllib.parse import urlsplit
from urllib.request import urlopen

url = sys.argv[1].rstrip("/")
parts = urlsplit(url)
if (parts.scheme not in ("https", "http") or not parts.hostname or parts.username
        or parts.password or parts.query or parts.fragment or any(c.isspace() for c in url)):
    sys.exit("Use an HTTP(S) distribution URL without credentials or query parameters.")
try:
    with urlopen(url + "/install.py", timeout=30) as response:
        source = response.read(128 * 1024 + 1)
    if len(source) > 128 * 1024:
        sys.exit("Installer exceeds the size limit.")
except OSError as error:
    sys.exit("Unable to download installer: " + str(error))
exec(compile(source, "miniagent-install", "exec"), {"__name__": "__main__"})
'@
$installArguments = $pythonArguments + @('-', $Url)
if ($Prefix) { $installArguments += @('--prefix', $Prefix) }
if ($BinDir) { $installArguments += @('--bin-dir', $BinDir) }
if ($AddToPath) { $installArguments += '--add-to-path' }
$bootstrap | & $pythonCommand @installArguments
if ($LASTEXITCODE -ne 0) { throw "MiniAgent installation failed (exit $LASTEXITCODE)." }

# Update this PowerShell process too; a child Python cannot change its parent.
if ($AddToPath) {
    if (-not $BinDir) { $BinDir = Join-Path $env:LOCALAPPDATA 'MiniAgent\bin' }
    $resolvedBin = [System.IO.Path]::GetFullPath($BinDir)
    if ($resolvedBin -notin ($env:Path -split ';')) {
        $env:Path = $resolvedBin + ';' + $env:Path
    }
}
