# Run with: pwsh -NoProfile -File tests/Test-ChessTableUsb.ps1
# These mocks exercise USB selection/attachment without touching the Uno.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '../scripts/ChessTableUsb.ps1')

$script:usbRows = @()
$script:usbCalls = @()
$script:wslCalls = @()

function usbipd {
    $script:usbCalls += ,@($args)
    $global:LASTEXITCODE = 0
    if ($args[0] -eq 'list') {
        return @('Connected:', 'BUSID  VID:PID    DEVICE                          STATE') + $script:usbRows
    }
    if ($args[0] -eq 'attach') {
        return 'Mock USB attach completed.'
    }
    throw "Unexpected usbipd call: $args"
}

function wsl.exe {
    $script:wslCalls += ,@($args)
    $null = @($input)
    $global:LASTEXITCODE = 0
    return '/dev/serial/by-id/mock-uno'
}

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

$script:usbRows = @('2-9    2341:0043  USB Serial Device (COM3)        Attached')
$device = Connect-ChessTableUsb -PythonPath '/usr/bin/python3' -WslPrefix @()
Assert-True ($device.Port -eq '/dev/serial/by-id/mock-uno') 'Attached Uno port was not returned.'
Assert-True ($script:usbCalls.Count -eq 1) 'Attached Uno should not trigger another usbipd attach.'
Assert-True ($script:wslCalls.Count -eq 1) 'Attached Uno should be identified in WSL.'

$script:usbCalls = @()
$script:wslCalls = @()
$script:usbRows = @('2-9    2341:0043  USB Serial Device (COM3)        Shared')
$device = Connect-ChessTableUsb -Distribution Ubuntu -WslPrefix @('-d', 'Ubuntu') -PythonPath '/usr/bin/python3'
Assert-True ($device.BusId -eq '2-9') 'Shared Uno bus was not returned.'
Assert-True ($script:usbCalls.Count -eq 2) 'Shared Uno should be listed and attached once.'
Assert-True (($script:usbCalls[1] -join ' ') -eq 'attach --wsl Ubuntu --busid 2-9') 'Shared Uno attach arguments are incorrect.'
Assert-True (($script:wslCalls[0] -join ' ') -eq '-d Ubuntu --exec /usr/bin/python3 - 2341:0043') 'Selected WSL distribution was not used.'

$script:usbCalls = @()
$script:wslCalls = @()
$device = Connect-ChessTableUsb -PythonPath '/usr/bin/python3' -DryRun
Assert-True ($device.Port -eq '<detected after USB attachment>') 'Dry-run placeholder port is incorrect.'
Assert-True ($script:usbCalls.Count -eq 1) 'Dry run must not attach USB.'
Assert-True ($script:wslCalls.Count -eq 0) 'Dry run should not probe an unattached serial port.'

$script:usbRows = @('2-9    2341:0043  USB Serial Device (COM3)        Not shared')
try {
    $null = Connect-ChessTableUsb -PythonPath '/usr/bin/python3'
    throw 'Unshared device was accepted.'
} catch {
    Assert-True ($_.Exception.Message -like '*usbipd bind --busid 2-9*') 'One-time admin bind guidance is missing.'
}

$script:usbRows = @(
    '2-9    2341:0043  Arduino Uno                     Shared',
    '2-11   2341:0043  Arduino Uno                     Shared'
)
try {
    $null = Get-ChessTableUsbDevice
    throw 'Ambiguous devices were accepted.'
} catch {
    Assert-True ($_.Exception.Message -like '*-UsbBusId*') 'Ambiguous device guidance is missing.'
}
$chosen = Get-ChessTableUsbDevice -BusId '2-11'
Assert-True ($chosen.BusId -eq '2-11') 'Explicit USB bus selection was ignored.'

Write-Host 'USB launcher mock tests passed.'
