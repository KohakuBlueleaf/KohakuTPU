# run_matmul_sim.ps1 -- correctness benches for the MX matmul datapath.
#
#   .\tests\run_matmul_sim.ps1              # both benches, both models
#   .\tests\run_matmul_sim.ps1 -Model 0     # DSP48E2 only
#
# Every bench is exact integer arithmetic checked bit-for-bit against a model
# computed in the bench itself. There is no tolerance to hide behind.
#
# Two models are run, and running BOTH is the point:
#
#   MODEL 1  behavioural  the arithmetic, with no primitive library involved
#   MODEL 0  DSP48E2      the real primitive, via -L unisims_ver
#
# A failure in 1 is a maths or wiring bug. A failure in 0 alone is a DSP
# configuration bug -- which is exactly how BREG=1 was caught: with AMULTSEL="AD"
# the A/D path takes two register stages to the multiplier and B took one, so B
# arrived a cycle early. Stable operands hide it; streaming does not.
#
# MODEL 0 needs glbl, which holds GSR asserted for the first 100 ns. The benches
# wait past it; without that the first tile silently produces nothing.
#
# Exit code 0 = pass.

param(
    [int[]]$Model = @(1, 0),
    [string]$VivadoBin = "D:\Xilinx\Vivado\2024.2\bin",
    [switch]$KeepWork
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$work = Join-Path $env:TEMP "kohakutpu-matmulsim"

$rtl = @(
    "src\kohakutpu\matmul\mx_mac.v",
    "src\kohakutpu\matmul\mx_tcu.v",
    "src\kohakutpu\matmul\mx_acu.v",
    "src\kohakutpu\matmul\mx_fpacc.v",
    "src\kohakutpu\matmul\mx_acu_fp.v",
    "src\kohakutpu\matmul\mx_cluster_core.v",
    "src\kohakutpu\matmul\mx_cluster.v",
    "src\kohakutpu\matmul\mx_tdesc.v",
    "src\common\sync_fifo.v",
    "src\common\kohaku_sdpram.v",
    "src\kohakutpu\matmul\mx_cluster_mgr.v",
    "src\kohakutpu\matmul\mx_cluster_node.v"
) | ForEach-Object { Join-Path $root $_ }

$benches = @(
    @{ Name = 'tensor CU (4x8x4, isolated)'
       Src  = @("tests\matmul\mx_tcu_tb.v"); Top = 'mx_tcu_tb' },
    @{ Name = 'cluster (4 TCU + 1 ACU, 4x32x4)'
       Src  = @("tests\matmul\mx_cluster_tb.v"); Top = 'mx_cluster_tb' },
    @{ Name = 'accumulator float primitives'
       Src  = @("tests\matmul\mx_fpacc_tb.v"); Top = 'mx_fpacc_tb'; Models = @(1) },
    @{ Name = 'accumulator CU (FP24, tile, peer)'
       Src  = @("tests\matmul\mx_acu_fp_tb.v"); Top = 'mx_acu_fp_tb'; Models = @(1) },
    @{ Name = 'tensor descriptor walker (matmul tile + conv2d im2col)'
       Src  = @("tests\matmul\mx_tdesc_tb.v"); Top = 'mx_tdesc_tb'; Models = @(1) },
    @{ Name = 'cluster node (mgr + chain + acu, 32x32x32 sweep)'
       Src  = @("tests\matmul\mx_cluster_node_tb.v"); Top = 'mx_cluster_node_tb' }
)

foreach ($f in $rtl) { if (-not (Test-Path $f)) { throw "missing source: $f" } }

if (Test-Path $work) { Remove-Item $work -Recurse -Force }
New-Item -ItemType Directory -Force $work | Out-Null
Push-Location $work
$failed = @()
try {
    $env:PATH = "$VivadoBin;$env:PATH"
    $glbl = Join-Path $VivadoBin "..\data\verilog\src\glbl.v"

    $k = 0
    foreach ($m in $Model) {
        foreach ($b in $benches) {
            if ($b.ContainsKey('Models') -and ($b.Models -notcontains $m)) { continue }
            $k++
            $tag = "m${m}_$k"
            Write-Host ""
            Write-Host "=============== $($b.Name) -- MODEL=$m ===============" -ForegroundColor Cyan

            # The model is selected by a source file, not -d MX_MODEL=0: Vivado's
            # .bat wrappers split arguments on '='. Same trap as MESH_N.
            $src = @()
            if ($m -eq 0) { $src += (Join-Path $root "tests\matmul\mx_model_dsp.v") }
            $src += $rtl
            $src += ($b.Src | ForEach-Object { Join-Path $root $_ })

            & xvlog.bat -sv -work "w$tag" $src 2>&1 |
                Where-Object { $_ -match 'ERROR' } | ForEach-Object { Write-Host $_ }
            if ($LASTEXITCODE -ne 0) { $failed += "$($b.Name) MODEL=$m (compile)"; continue }

            $tops = @("w$tag.$($b.Top)")
            # -L xpm: sync_fifo and kohaku_sdpram instantiate XPM macros, because
            # memory primitives are named rather than inferred.
            $libs = @("-L", "xpm")
            if ($m -eq 0) {
                & xvlog.bat -work "w$tag" $glbl 2>&1 |
                    Where-Object { $_ -match 'ERROR' } | ForEach-Object { Write-Host $_ }
                $tops += "w$tag.glbl"
                $libs += @("-L", "unisims_ver")
            }

            & xelab.bat -debug typical -timescale 1ns/1ps @libs @tops -s "tb$tag" 2>&1 |
                Where-Object { $_ -match 'ERROR' } | ForEach-Object { Write-Host $_ }
            if ($LASTEXITCODE -ne 0) { $failed += "$($b.Name) MODEL=$m (elaboration)"; continue }

            $out = & xsim.bat "tb$tag" -runall 2>&1
            $out | Where-Object { $_ -match '^---|^  |FAIL|PASS|=====|WATCHDOG' } |
                Where-Object { $_ -notmatch 'Build|Copyright|session' } |
                ForEach-Object { Write-Host $_ }

            if ($out -match 'WATCHDOG TIMEOUT') { $failed += "$($b.Name) MODEL=$m (watchdog)" }
            elseif ($out -match 'FAIL')         { $failed += "$($b.Name) MODEL=$m" }
            elseif (-not ($out -match 'PASS'))  { $failed += "$($b.Name) MODEL=$m (no verdict)" }
        }
    }

    Write-Host ""
    if ($failed.Count -gt 0) {
        Write-Host "FAILED: $($failed -join ', ')" -ForegroundColor Red
        exit 1
    }
    Write-Host "all matmul tests passed" -ForegroundColor Green
    exit 0
}
finally {
    Pop-Location
    if (-not $KeepWork -and (Test-Path $work)) {
        Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}
