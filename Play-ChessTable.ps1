<#
.SYNOPSIS
Starts an attended physical chess-table game with the Aina API enabled.
.DESCRIPTION
Creates a fresh physical checkpoint by default. Forwards optional parameters
such as -DryRun, -Port, -UsbBusId, -Engine, -Resume, and -State to the full
launcher. Keep this PowerShell window open while playing.
.EXAMPLE
.\Play-ChessTable.ps1
.EXAMPLE
.\Play-ChessTable.ps1 -DryRun
.EXAMPLE
.\Play-ChessTable.ps1 -Resume -State 'runs\chess-physical-YYYYMMDD-HHMMSS-XXXXXXXX.json'
#>
& (Join-Path $PSScriptRoot 'Start-ChessTable.ps1') -Mode Physical -EnableAina @args
