# run_interlink_sim.ps1 -- the MAG-to-MAG interlink, bottom up.
#
#   .\tests\run_interlink_sim.ps1                 # every bench
#   .\tests\run_interlink_sim.ps1 -Only link      # one of them
#
# The order is the order failures should be diagnosed in: a fault in the switch
# that the link bench already passes is a switch fault, and a four-mesh deadlock
# with both of those passing is a topology fault. Running them out of order buys
# nothing and costs the bisection.
#
#   link    one crossing, both credit classes, crossing latency swept 1..16
#   switch  XY on mesh coordinates, forwarding, isolation from local traffic
#   2mesh   the three transfer kinds, end to end, against real DRAM models
#   4mesh   adversarial: all-to-all saturation and asymmetric load
#
# Exit code 0 = pass.

param(
    [string]$Only = "",
    [string]$VivadoBin = "D:\Xilinx\Vivado\2024.2\bin",
    [switch]$KeepWork
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$work = Join-Path $env:TEMP "kohakutpu-ilsim"

$common = @(
    "src\common\sync_fifo.v",
    "src\common\kohaku_sdpram.v",
    "src\kohakumas\il_pkt_arb.v",
    "src\kohakumas\mag_link.v",
    "src\kohakumas\mag_link_pipe.v",
    "src\kohakumas\mag_switch.v"
)

$mesh = @(
    "src\kohakunoc\noc_inport.v",
    "src\kohakunoc\noc_outport.v",
    "src\kohakunoc\noc_router.v",
    "src\kohakunoc\noc_orchestrator.v",
    "src\kohakunoc\noc_cu_base.v",
    "src\kohakuaxi\axi_xbar2.v",
    "src\kohakuaxi\main_orch.v",
    "src\kohakutpu\vector\vec_agu.v",
    "src\kohakutpu\vector\vec_alu.v",
    "src\kohakutpu\vector\vec_core.v",
    "src\kohakutpu\vector\vec_cu.v",
    "src\kohakutpu\vector\vec_cvt.v",
    "src\kohakutpu\vector\vec_cvt_acc.v",
    "src\kohakutpu\vector\vec_delay.v",
    "src\kohakutpu\vector\vec_dsp.v",
    "src\kohakutpu\vector\vec_lanes.v",
    "src\kohakutpu\vector\vec_regfile.v",
    "src\kohakutpu\vector\vec_tables.v",
    "src\kohakutpu\matmul\mx_tdesc.v",
    "src\kohakumas\mm_prng.v",
    "src\kohakumas\mx_quant.v",
    "src\kohakumas\mm_mover.v",
    "src\kohakumas\mag_ilink.v",
    "src\kohakumas\mag_mem_port.v",
    "src\kohakumas\axi_ram.v",
    "src\kohakumas\mag.v"
)

$benches = @(
    @{ name = "link";   tb = "tests\mas\mag_link_tb.v";        top = "mag_link_tb";
       rtl = $common },
    @{ name = "switch"; tb = "tests\mas\mag_switch_tb.v";      top = "mag_switch_tb";
       rtl = $common },
    @{ name = "2mesh";  tb = "tests\mas\interlink_2mesh_tb.v"; top = "interlink_2mesh_tb";
       rtl = ($common + $mesh) },
    @{ name = "4mesh";  tb = "tests\mas\interlink_4mesh_tb.v"; top = "interlink_4mesh_tb";
       rtl = ($common + $mesh) }
)

if ($Only -ne "") { $benches = $benches | Where-Object { $_.name -eq $Only } }
if ($benches.Count -eq 0) { throw "no bench named '$Only'" }

if (Test-Path $work) { Remove-Item $work -Recurse -Force }
New-Item -ItemType Directory -Force $work | Out-Null
Push-Location $work
$failed = @()
try {
    $env:PATH = "$VivadoBin;$env:PATH"

    foreach ($b in $benches) {
        Write-Host ""
        Write-Host "=============== interlink -- $($b.name) ===============" -ForegroundColor Cyan

        $src = @()
        foreach ($f in $b.rtl) {
            $p = Join-Path $root $f
            if (-not (Test-Path $p)) { throw "missing source: $p" }
            $src += $p
        }
        $tb = Join-Path $root $b.tb
        if (-not (Test-Path $tb)) { throw "missing bench: $tb" }
        $src += $tb

        $lib = "w_$($b.name)"
        & xvlog.bat -sv -work $lib $src 2>&1 |
            Where-Object { $_ -match 'ERROR' } | ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0) { $failed += "$($b.name) (compile)"; continue }

        & xelab.bat -debug typical -timescale 1ns/1ps -L xpm "$lib.$($b.top)" `
            -s "sim_$($b.name)" 2>&1 |
            Where-Object { $_ -match 'ERROR' } | ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0) { $failed += "$($b.name) (elaboration)"; continue }

        $out = & xsim.bat "sim_$($b.name)" -runall 2>&1
        $out | Where-Object { $_ -match '^---|^  |^===|FAIL|PASS|WATCHDOG|ERROR' } |
            Where-Object { $_ -notmatch 'Build|Copyright|session|Time resolution' } |
            ForEach-Object { Write-Host $_ }

        if     ($out -match 'WATCHDOG')     { $failed += "$($b.name) (watchdog)" }
        elseif ($out -match 'FAIL')         { $failed += "$($b.name)" }
        elseif (-not ($out -match 'PASS'))  { $failed += "$($b.name) (no verdict)" }
    }

    Write-Host ""
    if ($failed.Count -gt 0) {
        Write-Host "FAILED: $($failed -join ', ')" -ForegroundColor Red
        exit 1
    }
    Write-Host "interlink tests passed" -ForegroundColor Green
    exit 0
}
finally {
    Pop-Location
    if (-not $KeepWork -and (Test-Path $work)) { Remove-Item $work -Recurse -Force }
}
