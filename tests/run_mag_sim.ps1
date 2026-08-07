# run_mag_sim.ps1 -- the whole partition, driven the way a driver would drive it.
#
#   .\tests\run_mag_sim.ps1              # behavioural and DSP48E2
#   .\tests\run_mag_sim.ps1 -Model 1     # behavioural only
#
#   fake AXI master (driver)  -+
#                              +- xbar -+- main orchestrator (slave)
#   main orchestrator (master) -+       +- MAG control
#                                            |
#                               MAG - AXI master -> axi_ram
#                                            |
#                                 2 NoC ports -> 2x2 mesh -> 2 clusters
#
# The host writes a control program (WR / POLL / DONE) into the main
# orchestrator and writes GO. Everything after that happens without the host.
#
# Needs -L xpm for the router FIFOs and the explicit memories; MODEL=0
# additionally needs unisims_ver and glbl.
#
# Exit code 0 = pass.

param(
    [int[]]$Model = @(1, 0),
    [string]$VivadoBin = "D:\Xilinx\Vivado\2024.2\bin",
    [switch]$KeepWork
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$work = Join-Path $env:TEMP "kohakutpu-magsim"

$rtl = @(
    "src\common\sync_fifo.v",
    "src\common\kohaku_sdpram.v",
    "src\kohakunoc\noc_inport.v",
    "src\kohakunoc\noc_outport.v",
    "src\kohakunoc\noc_router.v",
    "src\kohakunoc\noc_orchestrator.v",
    "src\kohakunoc\noc_cu_base.v",
    "src\kohakutpu\matmul\mx_mac.v",
    "src\kohakutpu\matmul\mx_tcu.v",
    "src\kohakutpu\matmul\mx_fpacc.v",
    "src\kohakutpu\matmul\mx_acu_fp.v",
    "src\kohakutpu\matmul\mx_cluster_core.v",
    "src\kohakutpu\matmul\mx_cluster_mgr.v",
    "src\kohakutpu\matmul\mx_cluster_node.v",
    "src\kohakutpu\matmul\mx_cluster_cu.v",
    "src\kohakuaxi\axi_xbar2.v",
    "src\kohakuaxi\main_orch.v",
    "src\kohakumas\axi_ram.v",
    "src\kohakumas\mag.v"
) | ForEach-Object { Join-Path $root $_ }

foreach ($f in $rtl) { if (-not (Test-Path $f)) { throw "missing source: $f" } }

if (Test-Path $work) { Remove-Item $work -Recurse -Force }
New-Item -ItemType Directory -Force $work | Out-Null
Push-Location $work
$failed = @()
try {
    $env:PATH = "$VivadoBin;$env:PATH"
    $glbl = Join-Path $VivadoBin "..\data\verilog\src\glbl.v"

    foreach ($m in $Model) {
        Write-Host ""
        Write-Host "=============== MAG partition -- MODEL=$m ===============" -ForegroundColor Cyan

        $src = @()
        if ($m -eq 0) { $src += (Join-Path $root "tests\matmul\mx_model_dsp.v") }
        $src += $rtl
        $src += (Join-Path $root "tests\mas\mag_system_tb.v")

        & xvlog.bat -sv -work "w$m" $src 2>&1 |
            Where-Object { $_ -match 'ERROR' } | ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0) { $failed += "MODEL=$m (compile)"; continue }

        $tops = @("w$m.mag_system_tb")
        $libs = @("-L", "xpm")
        if ($m -eq 0) {
            & xvlog.bat -work "w$m" $glbl 2>&1 |
                Where-Object { $_ -match 'ERROR' } | ForEach-Object { Write-Host $_ }
            $tops += "w$m.glbl"
            $libs += @("-L", "unisims_ver")
        }

        & xelab.bat -debug typical -timescale 1ns/1ps @libs @tops -s "tb$m" 2>&1 |
            Where-Object { $_ -match 'ERROR' } | ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0) { $failed += "MODEL=$m (elaboration)"; continue }

        $out = & xsim.bat "tb$m" -runall 2>&1
        $out | Where-Object { $_ -match '^---|^  |^    |FAIL|PASS|=====|WATCHDOG' } |
            Where-Object { $_ -notmatch 'Build|Copyright|session' } |
            ForEach-Object { Write-Host $_ }

        if ($out -match 'WATCHDOG')         { $failed += "MODEL=$m (watchdog)" }
        elseif ($out -match 'FAIL')         { $failed += "MODEL=$m" }
        elseif (-not ($out -match 'PASS'))  { $failed += "MODEL=$m (no verdict)" }
    }

    Write-Host ""
    if ($failed.Count -gt 0) {
        Write-Host "FAILED: $($failed -join ', ')" -ForegroundColor Red
        exit 1
    }
    Write-Host "MAG partition test passed" -ForegroundColor Green
    exit 0
}
finally {
    Pop-Location
    if (-not $KeepWork -and (Test-Path $work)) { Remove-Item $work -Recurse -Force }
}
