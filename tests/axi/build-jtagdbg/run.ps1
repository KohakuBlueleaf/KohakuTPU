# v6.7 JTAG repro. Sources are the frozen snapshot in .\rtl, never the tree.
param(
    [string[]]$Defs = @(),
    [string]$Tag = "lite",
    [string]$VivadoBin = "D:\Xilinx\Vivado\2024.2\bin"
)
$ErrorActionPreference = "Continue"
$here = $PSScriptRoot
Set-Location $here
$env:PATH = "$VivadoBin;$env:PATH"

$srcs = @(
    "rtl\async_fifo.v", "rtl\sync_fifo.v", "rtl\sb_skid.v",
    "rtl\sb_hub.v", "rtl\sb_station.v", "rtl\sb_nmu.v", "rtl\sb_nsu.v",
    "rtl\sb_link.v", "rtl\sb_link_cdc.v",
    "rtl\sb_stn_line.v", "rtl\sb_line4.v",
    "rtl\axi4_ram.v",
    "sb_v67_jtag_tb.v"
)
$w = "w_$Tag"
if (Test-Path $w) { Remove-Item $w -Recurse -Force }

$dargs = @()
foreach ($d in $Defs) { $dargs += "-d"; $dargs += $d }

& xvlog.bat -work $w @dargs @srcs 2>&1 |
    Where-Object { $_ -match 'ERROR|CRITICAL' } | ForEach-Object { Write-Host $_ }
if ($LASTEXITCODE -ne 0) { Write-Host "COMPILE FAILED"; exit 1 }

# xpm_cdc instantiates glbl, so the async FIFOs drag it in.
& xvlog.bat -work $w "$VivadoBin\..\data\verilog\src\glbl.v" 2>&1 |
    Where-Object { $_ -match 'ERROR|CRITICAL' } | ForEach-Object { Write-Host $_ }

& xelab.bat -debug typical -timescale 1ns/1ps -L xpm "$w.sb_v67_jtag_tb" "$w.glbl" -s "sim_$Tag" 2>&1 |
    Where-Object { $_ -match 'ERROR|CRITICAL' } | ForEach-Object { Write-Host $_ }
if ($LASTEXITCODE -ne 0) { Write-Host "ELABORATION FAILED"; exit 1 }

& xsim.bat "sim_$Tag" -runall 2>&1 | ForEach-Object { Write-Host $_ }
