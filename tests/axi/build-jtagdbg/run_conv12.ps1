# Conversion matrix: 12 permutations {full,lite}x{full,lite}x{M>S,M=S,M<S}
# plus 5 backbone-relative cases. Sanity-gates on ff_eq, then runs all cases
# in parallel (per-case dirs; xelab writes logs to CWD so sharing one dir
# corrupts concurrent builds). Sources: frozen snapshot in .\rtl.
param(
    [string]$VivadoBin = "D:\Xilinx\Vivado\2024.2\bin",
    [int]$MaxJobs = 8
)
$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
Set-Location $root
$env:PATH = "$VivadoBin;$env:PATH"

$srcNames = @(
    "rtl\async_fifo.v", "rtl\sync_fifo.v", "rtl\sb_skid.v",
    "rtl\sb_nmu.v", "rtl\sb_nsu.v",
    "rtl\sb_nmu_lite.v", "rtl\sb_nsu_lite.v", "rtl\sb_axi2lite.v",
    "rtl\axi4_ram.v",
    "sb_conv12_tb.v"
)
$srcs = $srcNames | ForEach-Object { Join-Path $root $_ }

$cases = @(
    @{tag="ff_gt"; defs=@("MW64")},
    @{tag="ff_eq"; defs=@()},
    @{tag="ff_lt"; defs=@("SW64")},
    @{tag="fl_gt"; defs=@("MW64","SUB_IS_LITE")},
    @{tag="fl_eq"; defs=@("SUB_IS_LITE")},
    @{tag="fl_lt"; defs=@("SW64","SUB_IS_LITE")},
    @{tag="lf_gt"; defs=@("MW64","MGR_IS_LITE")},
    @{tag="lf_eq"; defs=@("MGR_IS_LITE")},
    @{tag="lf_lt"; defs=@("SW64","MGR_IS_LITE")},
    @{tag="ll_gt"; defs=@("MW64","MGR_IS_LITE","SUB_IS_LITE")},
    @{tag="ll_eq"; defs=@("MGR_IS_LITE","SUB_IS_LITE")},
    @{tag="ll_lt"; defs=@("SW64","MGR_IS_LITE","SUB_IS_LITE")},
    @{tag="fx_m64_s256";  defs=@("MW64","SW256")},
    @{tag="fx_m256_s32";  defs=@("MW256")},
    @{tag="fx_m512_s256"; defs=@("MW512","SW256")},
    @{tag="fx_m512_l32";  defs=@("MW512","SUB_IS_LITE")},
    @{tag="fx_refuse_s512"; defs=@("SW512")}
)

$runCase = {
    param($tag, $defs, $root, $VivadoBin, $srcs)
    $env:PATH = "$VivadoBin;$env:PATH"
    $dir = Join-Path $root "par_$tag"
    if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }
    New-Item -ItemType Directory -Path $dir | Out-Null
    Set-Location $dir
    $dargs = @()
    foreach ($d in $defs) { $dargs += "-d"; $dargs += $d }
    $log = @("################ CASE $tag ($($defs -join ' ')) ################")
    $o = & xvlog.bat -work w_x @dargs @srcs 2>&1
    if ($LASTEXITCODE -ne 0) {
        $log += ($o | Where-Object { $_ -match 'ERROR' } | Select-Object -First 4)
        $log += "CASE $tag : COMPILE-FAILED"
        return $log
    }
    & xvlog.bat -work w_x "$VivadoBin\..\data\verilog\src\glbl.v" 2>&1 | Out-Null
    $o = & xelab.bat -debug typical -timescale 1ns/1ps -L xpm "w_x.sb_conv12_tb" "w_x.glbl" -s sim_x 2>&1
    if ($LASTEXITCODE -ne 0) {
        $log += ($o | Where-Object { $_ -match 'ERROR' } | Select-Object -First 4)
        $log += "CASE $tag : ELAB-FAILED"
        return $log
    }
    $o = & xsim.bat sim_x -runall 2>&1
    $log += ($o | Where-Object {
        $_ -match 'CHECK|HANG|VIOLATION|CASE|WATCHDOG|commit|NMU:|NSU:|STATE' })
    return $log
}

Write-Host "=== sanity: ff_eq serial ==="
$san = & $runCase "sanity" @() $root $VivadoBin $srcs
$san | ForEach-Object { Write-Host $_ }
if (-not ($san -match 'CASE-PASS')) {
    Write-Host "SANITY FAILED -- harness still broken, not launching the sweep"
    exit 1
}

Write-Host ""
Write-Host "=== parallel sweep: $($cases.Count) cases, max $MaxJobs jobs ==="
$jobs = @()
foreach ($c in $cases) {
    while ((Get-Job -State Running).Count -ge $MaxJobs) { Start-Sleep -Milliseconds 500 }
    $jobs += Start-Job -ScriptBlock $runCase -ArgumentList $c.tag, $c.defs, $root, $VivadoBin, $srcs
}
Wait-Job $jobs | Out-Null
$summary = @()
foreach ($j in $jobs) {
    $out = Receive-Job $j
    $out | ForEach-Object { Write-Host $_ }
    Write-Host ""
    $summary += ($out | Where-Object { $_ -match '^CASE ' })
}
Remove-Job $jobs

Write-Host "==================== SUMMARY ===================="
$summary | ForEach-Object { Write-Host $_ }
Write-Host "================================================="
