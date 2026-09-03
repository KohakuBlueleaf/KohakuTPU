# One multimesh image from start to bitstream, one Vivado at a time: block
# design (rebuild) -> verify (stops on a FAIL line) -> synthesis with the
# analysis -> impl through write_bitstream and the report. Stage marks go to
# the ship log below; each Vivado session keeps its own log beside the project.
#
#   pwsh -NoProfile -File scripts/ps1/v8t_ship.ps1 -Ver v8t7 -Jobs 8
param(
    [Parameter(Mandatory = $true)][string]$Ver,
    [int]$Jobs = 8
)
$root = "C:/Users/apoll/Desktop/code/Project/KohakuTPU"
$viv  = "D:/Xilinx/Vivado/2024.2/bin/vivado.bat"
$L    = "C:/Users/apoll/Desktop/vivado"
$stage = "$L/multimesh_${Ver}_ship.log"
Set-Location $root

function Mark([string]$s) { Add-Content -Path $stage -Value "$(Get-Date -Format 'HH:mm:ss')  $s" -Encoding utf8 }

Mark "bd: rebuild"
& $viv -mode batch -log "$L/multimesh_${Ver}_bd.log" -nojournal -notrace `
    -source "scripts/tcl/multimesh_${Ver}_bd.tcl" -tclargs rebuild jobs $Jobs | Out-Null
if ($LASTEXITCODE -ne 0) { Mark "BD FAILED ($LASTEXITCODE): $L/multimesh_${Ver}_bd.log"; exit 1 }

Mark "verify: 75_verify_bd"
$v = & $viv -mode batch -log "$L/multimesh_${Ver}_verify.log" -nojournal -notrace `
    -source "scripts/tcl/${Ver}_verify.tcl" 2>&1
$fails = @($v | Where-Object { $_ -match '@@@ FAIL' })
if ($fails.Count -gt 0 -or $LASTEXITCODE -ne 0) {
    Mark "VERIFY FAILED: $($fails.Count) FAIL line(s), exit $LASTEXITCODE -- $L/multimesh_${Ver}_verify.log"
    exit 1
}
Mark "verify: clean"

Mark "synth: OOC module runs, synth_1, analysis"
& $viv -mode batch -log "$L/multimesh_${Ver}_synth.log" -nojournal -notrace `
    -source "scripts/tcl/multimesh_${Ver}_bd.tcl" -tclargs synth jobs $Jobs analyze | Out-Null
if ($LASTEXITCODE -ne 0) { Mark "SYNTH FAILED ($LASTEXITCODE): $L/multimesh_${Ver}_synth.log"; exit 1 }

Mark "impl: through write_bitstream and the report"
& $viv -mode batch -log "$L/multimesh_${Ver}_impl.log" -nojournal -notrace `
    -source "scripts/tcl/${Ver}_impl.tcl" | Out-Null
if ($LASTEXITCODE -ne 0) { Mark "IMPL FAILED ($LASTEXITCODE): $L/multimesh_${Ver}_impl.log"; exit 1 }
Mark "SHIP DONE"
