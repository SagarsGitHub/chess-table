# Windows-to-WSL USB setup for Start-ChessTable.ps1. Dot-source this file; it
# defines functions but does not touch a USB device until Connect-ChessTableUsb
# is called.

function Get-ChessTableUsbDevice {
    param(
        [string]$BusId,
        [string]$HardwareId = '2341:0043'
    )

    if (-not (Get-Command usbipd -ErrorAction SilentlyContinue)) {
        throw 'usbipd-win is missing. Install it once with: winget install --interactive --exact dorssel.usbipd-win; then open a new PowerShell window.'
    }

    $listing = & usbipd list 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "usbipd list failed: $(($listing | Out-String).Trim())"
    }

    $devices = @(
        foreach ($line in $listing) {
            $lineText = [string]$line
            if ($lineText -match '^\s*(?<bus>\d+-\d+(?:\.\d+)*)\s+(?<id>[0-9a-fA-F]{4}:[0-9a-fA-F]{4})\s+.*?\s+(?<state>Not shared|Shared|Attached)\s*$') {
                [pscustomobject]@{
                    BusId = $Matches.bus
                    HardwareId = $Matches.id.ToLowerInvariant()
                    State = $Matches.state
                }
            }
        }
    )

    $candidates = @(if ($BusId) {
        $devices | Where-Object { $_.BusId -eq $BusId -and $_.HardwareId -eq $HardwareId.ToLowerInvariant() }
    } else {
        $devices | Where-Object { $_.HardwareId -eq $HardwareId.ToLowerInvariant() }
    })

    if ($candidates.Count -eq 0) {
        $selector = if ($BusId) { "bus $BusId with hardware ID $HardwareId" } else { "hardware ID $HardwareId" }
        throw "No connected USB device matches $selector. Check the Uno cable and run 'usbipd list'. For a different Uno VID:PID, pass -UsbHardwareId; to select one of several, pass -UsbBusId."
    }
    if ($candidates.Count -gt 1) {
        $ids = ($candidates | ForEach-Object { $_.BusId }) -join ', '
        throw "Multiple USB devices match $HardwareId ($ids). Pass -UsbBusId with the Uno's BUSID from 'usbipd list'."
    }
    return $candidates[0]
}

function Find-ChessTableWslSerialPort {
    param(
        [string]$HardwareId,
        [string[]]$WslPrefix = @(),
        [string]$PythonPath
    )

    # Send the probe through stdin so its quoting works in both Windows
    # PowerShell 5.1 and PowerShell 7 when forwarded by wsl.exe.
    $probe = @'
import glob
import os
import pathlib
import sys
import time

wanted = sys.argv[1].lower()

def hardware_id(sys_path):
    path = pathlib.Path(sys_path).resolve()
    for parent in (path, *path.parents):
        vendor = parent / 'idVendor'
        product = parent / 'idProduct'
        if vendor.is_file() and product.is_file():
            return vendor.read_text().strip().lower() + ':' + product.read_text().strip().lower()
    return None

def matching_ports():
    matches = []
    for pattern in ('/sys/class/tty/ttyACM*', '/sys/class/tty/ttyUSB*'):
        for sys_path in glob.glob(pattern):
            if hardware_id(pathlib.Path(sys_path) / 'device') == wanted:
                device = '/dev/' + pathlib.Path(sys_path).name
                if os.path.exists(device):
                    matches.append(device)
    return sorted(set(matches))

for attempt in range(25):
    ports = matching_ports()
    if ports:
        break
    time.sleep(0.2)
else:
    print('No matching WSL serial device appeared after USB attachment.', file=sys.stderr)
    sys.exit(3)

if len(ports) != 1:
    print('Multiple WSL serial devices match ' + wanted + ': ' + ', '.join(ports), file=sys.stderr)
    sys.exit(4)

target = os.path.realpath(ports[0])
by_id = sorted(
    path for path in glob.glob('/dev/serial/by-id/*')
    if os.path.islink(path) and os.path.realpath(path) == target
)
selected = by_id[0] if by_id else ports[0]
if not os.access(selected, os.R_OK | os.W_OK):
    print('Serial device is not readable and writable by this WSL user: ' + selected, file=sys.stderr)
    sys.exit(5)
print(selected)
'@

    $arguments = @($WslPrefix) + @('--exec', $PythonPath, '-', $HardwareId)
    $probeOutput = $probe | & wsl.exe @arguments 2>&1
    $probeExit = $LASTEXITCODE
    if ($probeExit -ne 0) {
        $detail = ($probeOutput | Out-String).Trim()
        if ($probeExit -eq 5) {
            throw "$detail Add your WSL user to the dialout group with 'sudo usermod -aG dialout `$USER', then restart WSL and retry."
        }
        throw "Could not identify the Uno serial port in WSL. $detail Check 'usbipd list' and 'ls -l /dev/serial/by-id /dev/ttyACM* /dev/ttyUSB*' in the selected distribution."
    }
    $port = ($probeOutput | Out-String).Trim()
    if ($port -notmatch '^/dev/(serial/by-id/[^\r\n]+|tty(?:ACM|USB)\d+)$') {
        throw "WSL returned an invalid serial device path: $port"
    }
    return $port
}

function Connect-ChessTableUsb {
    param(
        [string]$BusId,
        [string]$HardwareId = '2341:0043',
        [string]$Distribution,
        [string[]]$WslPrefix = @(),
        [string]$PythonPath,
        [switch]$DryRun
    )

    $device = Get-ChessTableUsbDevice -BusId $BusId -HardwareId $HardwareId
    if ($device.State -eq 'Not shared') {
        throw "Uno USB bus $($device.BusId) is not shared with WSL. In Administrator PowerShell, run 'usbipd bind --busid $($device.BusId)' once; then rerun this launcher in regular PowerShell."
    }

    if ($device.State -eq 'Shared') {
        if ($DryRun) {
            Write-Host "Dry run: would attach USB bus $($device.BusId) ($($device.HardwareId)) to WSL."
            return [pscustomobject]@{
                BusId = $device.BusId
                HardwareId = $device.HardwareId
                Port = '<detected after USB attachment>'
            }
        }
        $attachArguments = @('attach', '--wsl')
        if ($Distribution) { $attachArguments += $Distribution }
        $attachArguments += @('--busid', $device.BusId)
        $attachOutput = & usbipd @attachArguments 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "Could not attach Uno USB bus $($device.BusId) to WSL: $(($attachOutput | Out-String).Trim())"
        }
        foreach ($line in $attachOutput) { Write-Host $line }
    }

    $port = Find-ChessTableWslSerialPort -HardwareId $device.HardwareId -WslPrefix $WslPrefix -PythonPath $PythonPath
    return [pscustomobject]@{
        BusId = $device.BusId
        HardwareId = $device.HardwareId
        Port = $port
    }
}
