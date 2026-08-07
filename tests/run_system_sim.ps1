# run_system_sim.ps1 -- end-to-end matmul on a 1x5 NoC.
#
#   .\tests\run_system_sim.ps1              # behavioural and DSP48E2
#   .\tests\run_system_sim.ps1 -Model 0     # DSP48E2 only
#
# Exercises the whole control and data path with nothing stubbed but DRAM:
#
#   host stages a program over AXI  ->  orchestrator dispatches CU_INST
#   -> CU issues MEM_RD_REQ naming itself as src
#   -> memory replies straight to the CU
#   -> cluster computes, ACU accumulates in FP24
#   -> CU writes the FP16 result back and signals completion
#   -> orchestrator mirrors it into NODE_STATUS, host polls over AXI
#
# The bench reports precision against FP64 and against a CPU int7 model, and
# checks the two agree with each other before trusting either.
#
# Needs -L xpm for the router's FIFOs; MODEL=0 additionally needs unisims_ver
# and glbl.
#
# Exit code 0 = pass.

# SUPERSEDED, and kept only until someone confirms nothing depends on it.
# scripts\py\xsim.py runs the same benches, and it names sources in ONE table so
# that adding a file is one edit rather than one per caller. This script keeps a
# hand-maintained duplicate of that list, and the duplicate is exactly how it
# broke: noc_fake_mem.v gained an mx_quant instance and this list never learned
# about it, so all three benches failed elaboration on an unresolved module
# while xsim.py kept working. Prefer:
#     driver\.venv\Scripts\python.exe scripts\py\xsim.py system
param(
    [int[]]$Model = @(1, 0),
    [string[]]$Bench = @("mx_system_tb", "mx_system32_tb"),
    [string]$VivadoBin = "D:\Xilinx\Vivado\2024.2\bin",
    [switch]$KeepWork
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$work = Join-Path $env:TEMP "kohakutpu-syssim"

$rtl = @(
    "src\common\sync_fifo.v",
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
    "src\kohakutpu\matmul\mx_matmul_cu.v",
    "src\common\kohaku_sdpram.v",
    "src\kohakutpu\matmul\mx_tdesc.v",
    "src\kohakutpu\matmul\mx_cluster_mgr.v",
    "src\kohakutpu\matmul\mx_cluster_node.v",
    "src\kohakutpu\matmul\mx_cluster_cu.v",
    # noc_fake_mem instantiates the quantiser: memory holds FP16 and the CU asks
    # for MXFP7, so a stub without it hands back raw FP16 as operands.
    "src\kohakumas\mx_quant.v",
    "tests\noc\noc_fake_mem.v"
) | ForEach-Object { Join-Path $root $_ }

foreach ($f in $rtl) { if (-not (Test-Path $f)) { throw "missing source: $f" } }

if (Test-Path $work) { Remove-Item $work -Recurse -Force }
New-Item -ItemType Directory -Force $work | Out-Null
Push-Location $work
$failed = @()
try {
    $env:PATH = "$VivadoBin;$env:PATH"
    $glbl = Join-Path $VivadoBin "..\data\verilog\src\glbl.v"

    foreach ($tb in $Bench) {
      foreach ($m in $Model) {
        $tag = "$tb MODEL=$m"
        $w   = "w_${tb}_$m"
        Write-Host ""
        Write-Host "=============== $tb -- MODEL=$m ===============" -ForegroundColor Cyan

        # Model selected by a source file, not -d MX_MODEL=0: Vivado's .bat
        # wrappers split arguments on '='.
        $src = @()
        if ($m -eq 0) { $src += (Join-Path $root "tests\matmul\mx_model_dsp.v") }
        $src += $rtl
        $src += (Join-Path $root "tests\noc\$tb.v")

        & xvlog.bat -sv -work $w $src 2>&1 |
            Where-Object { $_ -match 'ERROR' } | ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0) { $failed += "$tag (compile)"; continue }

        $tops = @("$w.$tb")
        $libs = @("-L", "xpm")
        if ($m -eq 0) {
            & xvlog.bat -work $w $glbl 2>&1 |
                Where-Object { $_ -match 'ERROR' } | ForEach-Object { Write-Host $_ }
            $tops += "$w.glbl"
            $libs += @("-L", "unisims_ver")
        }

        & xelab.bat -debug typical -timescale 1ns/1ps @libs @tops -s "sim_${tb}_$m" 2>&1 |
            Where-Object { $_ -match 'ERROR' } | ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0) { $failed += "$tag (elaboration)"; continue }

        $out = & xsim.bat "sim_${tb}_$m" -runall 2>&1
        $out | Where-Object { $_ -match '^---|^  |^    |^ *-?[0-9]|FAIL|PASS|=====|WATCHDOG' } |
            Where-Object { $_ -notmatch 'Build|Copyright|session' } |
            ForEach-Object { Write-Host $_ }

        if ($out -match 'WATCHDOG TIMEOUT') { $failed += "$tag (watchdog)" }
        elseif ($out -match 'FAIL')         { $failed += $tag }
        elseif (-not ($out -match 'PASS'))  { $failed += "$tag (no verdict)" }
      }
    }

    Write-Host ""
    if ($failed.Count -gt 0) {
        Write-Host "FAILED: $($failed -join ', ')" -ForegroundColor Red
        exit 1
    }
    Write-Host "system test passed" -ForegroundColor Green
    exit 0
}
finally {
    Pop-Location
    if (-not $KeepWork -and (Test-Path $work)) {
        Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}
