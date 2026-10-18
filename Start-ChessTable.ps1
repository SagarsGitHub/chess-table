<#
.SYNOPSIS
Starts the checkpointed chess-table GUI in WSL.
.DESCRIPTION
Creates a new simulation by default. Use -Mode Physical to locate and attach
the Uno to WSL automatically, or pass -Port for a known WSL serial device.
-EnableAina adds the authenticated loopback API; the token file and Aina
companion must be configured separately. If -Engine is omitted and Windows
Documents/Stockfish exists, that folder is passed to the WSL host.
.EXAMPLE
.\Start-ChessTable.ps1 -EnableAina
.EXAMPLE
.\Start-ChessTable.ps1 -Mode Physical -EnableAina
.EXAMPLE
.\Start-ChessTable.ps1 -Mode Physical -UsbBusId 2-9 -EnableAina
.EXAMPLE
.\Start-ChessTable.ps1 -Mode Physical -Resume -State 'runs\chess-physical-20261010-120000-a1b2c3d4.json' -EnableAina
#>
[CmdletBinding()]
param(
    [ValidateSet('Simulation', 'Physical')]
    [string]$Mode = 'Simulation',

    [switch]$Resume,
    [string]$State,
    [string]$Port,
    [ValidatePattern('^\d+-\d+(?:\.\d+)*$')]
    [string]$UsbBusId,
    [ValidatePattern('^[0-9a-fA-F]{4}:[0-9a-fA-F]{4}$')]
    [string]$UsbHardwareId = '2341:0043',
    [switch]$EnableAina,
    [string]$AinaTokenFile,
    [ValidateRange(1, 65535)]
    [int]$GuiPort = 8765,
    [ValidateRange(1, 65535)]
    [int]$AinaPort = 8766,
    [switch]$EnablePocketMage,
    [string]$PocketMageBind = '0.0.0.0',
    [ValidateRange(1, 65535)]
    [int]$PocketMagePort = 8767,
    [string]$Distribution,
    [string]$Engine,
    [switch]$FullHome,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$computerDirectory = Join-Path $PSScriptRoot 'computer'
$wslPrefix = @()
if ($Distribution) {
    $wslPrefix += @('-d', $Distribution)
}

function Invoke-WslQuery {
    param([string[]]$Command)
    $arguments = @($wslPrefix) + @('--exec') + $Command
    $output = & wsl.exe @arguments
    if ($LASTEXITCODE -ne 0) {
        throw 'WSL query failed. Check that Ubuntu is installed and the selected distribution is running.'
    }
    return ($output | Out-String).Trim()
}

function ConvertTo-WslPath {
    param([string]$WindowsPath)
    return Invoke-WslQuery -Command @('wslpath', '-a', $WindowsPath)
}

function Test-WslPath {
    param([string]$Check, [string]$WslPath)
    $arguments = @($wslPrefix) + @('--exec', '/usr/bin/test', $Check, $WslPath)
    & wsl.exe @arguments
    return $LASTEXITCODE -eq 0
}

function Assert-WslLoopbackPortAvailable {
    param([int]$CheckPort, [string]$Purpose, [string]$PortOption, [string]$BindAddress = '127.0.0.1')

    # Probe in the selected WSL distribution, where the Python host binds.
    # Match the HTTP servers' SO_REUSEADDR setting so a recently closed port
    # is not mistaken for an active listener.
    $probe = @'
import socket
import sys
try:
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((sys.argv[2], int(sys.argv[1])))
except OSError:
    sys.exit(1)
'@
    $arguments = @($wslPrefix) + @('--exec', $pythonPath, '-c', $probe, [string]$CheckPort, $BindAddress)
    & wsl.exe @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Purpose port $CheckPort is unavailable in WSL. Stop the existing chess host or other listener, or choose another port with $PortOption. No new game was started."
    }
}

if ($Resume -and -not $PSBoundParameters.ContainsKey('Mode')) {
    throw 'Choose -Mode Simulation or -Mode Physical explicitly when resuming a checkpoint.'
}
if ($Resume -and -not $State) {
    throw '-Resume requires -State with the existing checkpoint path.'
}
if ($Mode -eq 'Physical') {
    if ($Port -and -not $Port.StartsWith('/dev/')) {
        throw '-Port must be an attached WSL serial device path, such as /dev/serial/by-id/... or /dev/ttyACM0.'
    }
} elseif ($Port) {
    throw '-Port is only valid with -Mode Physical.'
}
if ($Mode -ne 'Physical' -and ($UsbBusId -or $PSBoundParameters.ContainsKey('UsbHardwareId'))) {
    throw '-UsbBusId and -UsbHardwareId are only valid with -Mode Physical.'
}
if ($Port -and ($UsbBusId -or $PSBoundParameters.ContainsKey('UsbHardwareId'))) {
    throw '-Port selects the WSL device directly; omit -UsbBusId and -UsbHardwareId.'
}
if ($FullHome -and $Mode -ne 'Physical') {
    throw '-FullHome is only valid with -Mode Physical.'
}
if ($AinaTokenFile -and -not $EnableAina) {
    throw '-AinaTokenFile requires -EnableAina.'
}
if ($EnableAina -and $GuiPort -eq $AinaPort) {
    throw '-GuiPort and -AinaPort must differ.'
}
if ($EnablePocketMage -and ($PocketMagePort -eq $GuiPort -or ($EnableAina -and $PocketMagePort -eq $AinaPort))) {
    throw '-PocketMagePort must differ from the GUI and enabled Aina ports.'
}
$pocketMageAddress = $null
if (-not [Net.IPAddress]::TryParse($PocketMageBind, [ref]$pocketMageAddress) -or
    $pocketMageAddress.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
    throw '-PocketMageBind must be a literal IPv4 address.'
}
if ($PSBoundParameters.ContainsKey('Engine') -and -not $Engine) {
    throw '-Engine must be a nonempty Stockfish executable or directory path.'
}
if (-not (Test-Path -LiteralPath $computerDirectory -PathType Container)) {
    throw "Cannot find the computer checkout at $computerDirectory."
}

if (-not $State) {
    $kind = if ($Mode -eq 'Simulation') { 'sim' } else { 'physical' }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $suffix = [guid]::NewGuid().ToString('N').Substring(0, 8)
    $State = "runs/chess-$kind-$stamp-$suffix.json"
}
$stateWindowsPath = if ([IO.Path]::IsPathRooted($State)) {
    [IO.Path]::GetFullPath($State)
} else {
    [IO.Path]::GetFullPath((Join-Path $computerDirectory $State))
}
if ($Resume) {
    if (-not (Test-Path -LiteralPath $stateWindowsPath -PathType Leaf)) {
        throw "Cannot resume: checkpoint does not exist at $stateWindowsPath."
    }
    $scriptStateKind = [IO.Path]::GetFileName($stateWindowsPath)
    if ($scriptStateKind -match '^chess-(sim|physical)-') {
        $expectedKind = if ($Mode -eq 'Simulation') { 'sim' } else { 'physical' }
        if ($Matches[1] -ne $expectedKind) {
            throw "Checkpoint filename indicates a different mode; choose the matching -Mode for $stateWindowsPath."
        }
    }
} elseif (Test-Path -LiteralPath $stateWindowsPath) {
    throw "Cannot start a new game: checkpoint already exists at $stateWindowsPath. Use -Resume with an explicit -Mode."
}

$wslComputerDirectory = ConvertTo-WslPath $computerDirectory
$wslStatePath = ConvertTo-WslPath $stateWindowsPath
$pythonPath = "$wslComputerDirectory/.venv/bin/python"
if (-not (Test-WslPath '-x' $pythonPath)) {
    throw "WSL Python environment is missing at $pythonPath. Install computer/requirements.txt in computer/.venv first."
}

if (-not $PSBoundParameters.ContainsKey('Engine') -and $env:USERPROFILE) {
    $windowsStockfish = Join-Path $env:USERPROFILE 'Documents/Stockfish'
    if (Test-Path -LiteralPath $windowsStockfish -PathType Container) {
        # Resolve the actual directory spelling before crossing into WSL.
        $Engine = (Get-Item -LiteralPath $windowsStockfish).FullName
    }
}

$hostArguments = @(
    '-m', 'chess_table_host.gui',
    $(if ($Resume) { '--resume' } else { '--new' }),
    '--state', $wslStatePath,
    '--http-port', [string]$GuiPort
)
if ($Mode -eq 'Simulation') {
    $geometryWindowsPath = Join-Path $computerDirectory 'fixtures/board_geometry_2026-10-05.json'
    if (-not (Test-Path -LiteralPath $geometryWindowsPath -PathType Leaf)) {
        throw "Simulation geometry fixture is missing at $geometryWindowsPath."
    }
    $hostArguments += @('--simulate', '--geometry-file', (ConvertTo-WslPath $geometryWindowsPath))
} else {
    if ($FullHome) { $hostArguments += '--home' }
}
$enginePath = $null
if ($Engine) {
    $enginePath = if ([IO.Path]::IsPathRooted($Engine) -and -not $Engine.StartsWith('/')) {
        ConvertTo-WslPath $Engine
    } else {
        $Engine
    }
    $hostArguments += @('--engine', $enginePath)
}

if ($EnableAina) {
    if (-not $AinaTokenFile) {
        $wslUserHome = Invoke-WslQuery -Command @('printenv', 'HOME')
        if (-not $wslUserHome.StartsWith('/')) {
            throw 'Could not find the WSL user home for the Aina token file.'
        }
        $AinaTokenFile = "$wslUserHome/.config/chess-table/aina.token"
    } elseif (-not $AinaTokenFile.StartsWith('/')) {
        if (-not [IO.Path]::IsPathRooted($AinaTokenFile)) {
            throw '-AinaTokenFile must be an absolute WSL or Windows path.'
        }
        $AinaTokenFile = ConvertTo-WslPath $AinaTokenFile
    }
    if (-not (Test-WslPath '-r' $AinaTokenFile)) {
        throw "Aina token file is not readable at $AinaTokenFile. See docs/REMOTE_AINA.md for one-time authentication setup."
    }
    $hostArguments += @('--remote-token-file', $AinaTokenFile,
                        '--remote-http-port', [string]$AinaPort)
}

Assert-WslLoopbackPortAvailable -CheckPort $GuiPort -Purpose 'GUI' -PortOption '-GuiPort'
if ($EnablePocketMage) {
    Assert-WslLoopbackPortAvailable -CheckPort $PocketMagePort -Purpose 'PocketMage API' -PortOption '-PocketMagePort' -BindAddress $PocketMageBind
    $hostArguments += @('--enable-pocketmage', '--pocketmage-bind', $PocketMageBind,
                        '--pocketmage-port', [string]$PocketMagePort)
}
if ($EnableAina) {
    Assert-WslLoopbackPortAvailable -CheckPort $AinaPort -Purpose 'Aina API' -PortOption '-AinaPort'
}

if ($Mode -eq 'Physical') {
    if (-not $Port) {
        . (Join-Path $PSScriptRoot 'scripts/ChessTableUsb.ps1')
        $usbConnection = Connect-ChessTableUsb -BusId $UsbBusId `
            -HardwareId $UsbHardwareId -Distribution $Distribution `
            -WslPrefix $wslPrefix -PythonPath $pythonPath -DryRun:$DryRun
        $Port = $usbConnection.Port
        Write-Host "Uno USB: bus $($usbConnection.BusId), $($usbConnection.HardwareId)"
    }
    $hostArguments += @('--port', $Port)
}

Write-Host "Checkpoint: $stateWindowsPath"
Write-Host "Mode: $Mode"
if ($enginePath) { Write-Host "Stockfish: $enginePath" }
Write-Host "Local GUI: http://localhost:$GuiPort/"
if ($EnablePocketMage) {
    $pocketMageDestination = $PocketMageBind
    if ($pocketMageDestination -eq '0.0.0.0') {
        $pocketMageDestination = Invoke-WslQuery -Command @($pythonPath, '-c', 'import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(("192.0.2.1",9)); print(s.getsockname()[0]); s.close()')
    }
    Write-Host "PocketMage API: http://${pocketMageDestination}:$PocketMagePort (trusted LAN, no token)"
    Write-Host 'For WSL hosting, confirm this address is reachable from the device LAN.'
}
if ($EnableAina) {
    Write-Host "Aina API: http://127.0.0.1:$AinaPort/v1/status"
    Write-Host "Aina token file: $AinaTokenFile (value hidden)"
}
if ($Mode -eq 'Physical') {
    Write-Host "Uno port: $Port; keep the power cutoff reachable."
}
if ($DryRun) {
    Write-Host 'Dry run: host was not started.'
    return
}

$launchArguments = @($wslPrefix) + @('--cd', $wslComputerDirectory, '--exec',
                                    '.venv/bin/python') + $hostArguments
& wsl.exe @launchArguments
if ($LASTEXITCODE -ne 0) {
    throw "Chess host exited with code $LASTEXITCODE. Preserve the checkpoint and inspect the error above."
}
