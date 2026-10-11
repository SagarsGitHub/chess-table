# Run with: pwsh -NoProfile -File tests/Test-PlayChessTable.ps1
# All WSL and usbipd commands are mocked; no chess host or hardware is run.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

$global:launcherWslCalls = @()
$global:launcherUsbCalls = @()
function wsl.exe {
    $call = @($args)
    $global:launcherWslCalls += ,$call
    $global:LASTEXITCODE = 0
    if ($call -contains 'wslpath') {
        return '/mock/' + [IO.Path]::GetFileName([string]$call[-1])
    }
    if ($call -contains 'printenv') { return '/home/mock' }
    if ($call -contains '-') {
        $null = @($input)
        return '/dev/serial/by-id/mock-uno'
    }
}
function usbipd {
    $global:launcherUsbCalls += ,@($args)
    $global:LASTEXITCODE = 0
    if ($args[0] -eq 'list') {
        return @(
            'Connected:',
            'BUSID  VID:PID    DEVICE                          STATE',
            '2-9    2341:0043  USB Serial Device (COM3)        Attached'
        )
    }
    throw 'A dry run with an attached Uno must not call usbipd attach.'
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$fixtureRoot = Join-Path ([IO.Path]::GetTempPath()) ('chess-table-launcher-test-' + [guid]::NewGuid().ToString('N'))
$savedUserProfile = $env:USERPROFILE
try {
    $stockfishFolder = Join-Path $fixtureRoot 'Documents/Stockfish'
    $null = New-Item -ItemType Directory -Path $stockfishFolder -Force
    $env:USERPROFILE = $fixtureRoot

    $global:launcherWslCalls = @()
    $null = & (Join-Path $repoRoot 'Start-ChessTable.ps1') -GuiPort 18765
    $launch = @($global:launcherWslCalls[-1])
    $engineIndex = [array]::IndexOf($launch, '--engine')
    Assert-True ($engineIndex -ge 0) 'Default Windows Stockfish folder was not passed to the host.'
    Assert-True ($launch[$engineIndex + 1] -eq '/mock/Stockfish') 'Default Stockfish path was not converted to WSL.'

    $global:launcherWslCalls = @()
    $null = & (Join-Path $repoRoot 'Start-ChessTable.ps1') -GuiPort 18765 -Engine '/custom/stockfish'
    $launch = @($global:launcherWslCalls[-1])
    $engineIndex = [array]::IndexOf($launch, '--engine')
    Assert-True ($launch[$engineIndex + 1] -eq '/custom/stockfish') 'Explicit -Engine did not override the default.'

    $global:launcherWslCalls = @()
    $global:launcherUsbCalls = @()
    $output = & (Join-Path $repoRoot 'Play-ChessTable.ps1') -GuiPort 18765 -AinaPort 18766 -DryRun 6>&1 | Out-String
    Assert-True ($output -like '*Mode: Physical*') 'Daily wrapper did not select physical mode.'
    Assert-True ($output -like '*Aina API:*') 'Daily wrapper did not enable Aina.'
    Assert-True ($output -like '*Stockfish: /mock/Stockfish*') 'Daily wrapper did not select default Stockfish.'
    Assert-True ($output -like '*Uno port: /dev/serial/by-id/mock-uno*') 'Daily wrapper did not resolve Uno serial port.'
    Assert-True ($global:launcherUsbCalls.Count -eq 1) 'Daily dry run should list USB once and never attach.'
    $hostCalls = @($global:launcherWslCalls | Where-Object { $_ -contains 'chess_table_host.gui' })
    Assert-True ($hostCalls.Count -eq 0) 'Daily dry run must not start the chess host.'

    $resumeFile = Join-Path $fixtureRoot 'chess-physical-test.json'
    [IO.File]::WriteAllText($resumeFile, '{}')
    $global:launcherUsbCalls = @()
    $output = & (Join-Path $repoRoot 'Play-ChessTable.ps1') -Resume -State $resumeFile `
        -Port '/dev/ttyACM9' -GuiPort 18765 -AinaPort 18766 -DryRun 6>&1 | Out-String
    Assert-True ($output -like '*Uno port: /dev/ttyACM9*') 'Daily wrapper did not forward -Port.'
    Assert-True ($output -like '*Checkpoint:*chess-physical-test.json*') 'Daily wrapper did not forward -Resume and -State.'
    Assert-True ($global:launcherUsbCalls.Count -eq 0) 'Explicit -Port must bypass USB discovery.'

    Write-Host 'Daily launcher mock tests passed.'
} finally {
    $env:USERPROFILE = $savedUserProfile
    $resolvedFixture = [IO.Path]::GetFullPath($fixtureRoot)
    $resolvedTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if ($resolvedFixture.StartsWith($resolvedTemp, [StringComparison]::OrdinalIgnoreCase) -and
        [IO.Path]::GetFileName($resolvedFixture).StartsWith('chess-table-launcher-test-')) {
        Remove-Item -LiteralPath $resolvedFixture -Recurse -Force -ErrorAction SilentlyContinue
    }
}
