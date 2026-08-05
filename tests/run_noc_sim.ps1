# run_noc_sim.ps1 -- simulate the NoC mesh.
#
#   .\tests\run_noc_sim.ps1              # 2x2 and 3x3
#   .\tests\run_noc_sim.ps1 -Size 4      # one specific mesh size
#
# Uses Vivado's xsim: the router instantiates xpm_fifo_sync, so the XPM library
# has to be linked in, which iverilog cannot do.
#
# What a pass means: over the traffic actually exercised, nothing was lost,
# everything landed at the node it was addressed to, per-pair order held, and the
# run finished. Deadlock-freedom itself comes from XY routing being acyclic by
# construction (docs/noc/spec.md s2) -- no finite test can establish it, so a pass
# here is corroboration, not proof.
#
# Exit code 0 = pass.

param(
    [int[]]$Size = @(2, 3),
    [string]$VivadoBin = "D:\Xilinx\Vivado\2024.2\bin",
    [switch]$KeepWork
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$work = Join-Path $env:TEMP "hakutpu-nocsim"

$sources = @(
    "src\common\sync_fifo.v",
    "src\hakunoc\noc_inport.v",
    "src\hakunoc\noc_outport.v",
    "src\hakunoc\noc_router.v",
    "tests\noc\noc_mesh_tb.v"
) | ForEach-Object { Join-Path $root $_ }

foreach ($s in $sources) { if (-not (Test-Path $s)) { throw "missing source: $s" } }

if (Test-Path $work) { Remove-Item $work -Recurse -Force }
New-Item -ItemType Directory -Force $work | Out-Null
Push-Location $work
$failed = @()
try {
    $env:PATH = "$VivadoBin;$env:PATH"

    foreach ($n in $Size) {
        Write-Host ""
        Write-Host "=============== ${n}x${n} mesh ===============" -ForegroundColor Cyan

        # Mesh size is a compile-time define, so each size gets its own analysis
        # into its own library. The define is written to a source file rather than
        # passed as -d MESH_N=n, because Vivado's .bat wrappers split arguments on
        # '=' (which also defeats xelab -generic_top).
        $cfg = Join-Path $work "mesh_n_$n.v"
        Set-Content $cfg "``define MESH_N $n"
        & xvlog.bat -sv -work "w$n" (@($cfg) + $sources) 2>&1 |
            Where-Object { $_ -match 'ERROR' } | ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0) { $failed += "${n}x${n} (compile)"; continue }

        & xelab.bat -debug typical -timescale 1ns/1ps -L xpm `
                    "w$n.noc_mesh_tb" -s "tb_noc_$n" 2>&1 |
            Where-Object { $_ -match 'ERROR' } | ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0) { $failed += "${n}x${n} (elaboration)"; continue }

        $out = & xsim.bat "tb_noc_$n" -runall 2>&1
        $out | Where-Object {
            $_ -match 'phase|ERROR|MISDELIVERY|OUT OF ORDER|sent |PASS|FAIL|WATCHDOG|====='
        } | ForEach-Object { Write-Host $_ }

        if ($out -match 'WATCHDOG TIMEOUT') {
            Write-Host "  deadlock or lost backpressure -- check the in-flight count above" -ForegroundColor Red
            $failed += "${n}x${n} (watchdog)"
        }
        elseif ($out -match 'FAIL') { $failed += "${n}x${n}" }
        elseif (-not ($out -match 'PASS')) { $failed += "${n}x${n} (no verdict)" }
    }

    Write-Host ""
    if ($failed.Count -gt 0) {
        Write-Host "FAILED: $($failed -join ', ')" -ForegroundColor Red
        exit 1
    }
    Write-Host "all mesh sizes passed" -ForegroundColor Green
    exit 0
}
finally {
    Pop-Location
    if (-not $KeepWork -and (Test-Path $work)) {
        Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}
