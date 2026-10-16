# Waits for impl_1 to finish place_design after this script starts, then runs
# the placed-checkpoint measurement (scripts/tcl/v8t3/89_placed.tcl) in its own Vivado.
#   pwsh -NoProfile -File scripts/ps1/v8t_placed.ps1 -Ver v9t5
param([Parameter(Mandatory = $true)][string]$Ver)
$root = "C:/Users/apoll/Desktop/code/Project/KohakuTPU"
$viv  = "D:/Xilinx/Vivado/2024.2/bin/vivado.bat"
$L    = "C:/Users/apoll/Desktop/vivado"
$stage = "$L/multimesh_${Ver}_ship.log"
$rd   = "$L/multimesh_${Ver}/multimesh_${Ver}.runs/impl_1"
$dcp  = "$rd/multimesh_${Ver}_wrapper_placed.dcp"
Set-Location $root

function Mark([string]$s) { Add-Content -Path $stage -Value "$(Get-Date -Format 'HH:mm:ss')  $s" -Encoding utf8 }
function Newer([string]$p, [datetime]$t) { (Test-Path $p) -and (Get-Item $p -Force).LastWriteTime -gt $t }

$t0 = Get-Date
while (-not ((Newer "$rd/.place_design.end.rst" $t0) -and (Newer $dcp $t0))) {
    if (Newer "$rd/.place_design.error.rst" $t0) { Mark "placed: place_design failed, no measurement"; exit 1 }
    Start-Sleep -Seconds 60
}
Mark "placed: measurement started, $L/multimesh_${Ver}_placed.log"
& $viv -mode batch -log "$L/multimesh_${Ver}_placed.log" -nojournal -notrace `
    -source "scripts/tcl/v8t3/89_placed.tcl" -tclargs "scripts/tcl/${Ver}/00_config.tcl" $dcp | Out-Null
Mark "placed: measurement done (exit $LASTEXITCODE)"
exit $LASTEXITCODE
