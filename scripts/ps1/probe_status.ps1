<#
.SYNOPSIS
    Show how far each Vivado probe (and the main-project impl) has got.

.DESCRIPTION
    One line per run: stage, current phase, elapsed, idle, WNS/TNS with a
    movement arrow against the previous refresh, hold, congestion, and that
    run's resident memory. Reads only log TAILS and caches by mtime+length, so
    a refresh touches kilobytes, not the multi-hundred-MB runme.log files.

    The idle column is the stall signal: Vivado writes progress continuously,
    so a log that has not moved for many minutes is stalled, not slow.

.PARAMETER Root
    Directory holding one subdirectory per probe.

.PARAMETER Main
    A .runs\<run> directory to show as an extra 'main' row (the v6 production
    impl by default). '' disables it.

.PARAMETER TailLines
    How many lines of each log tail to parse. Phases and timing lines repeat
    every few hundred lines, so 600 is comfortably enough.

.PARAMETER Watch / Every / Errors / Filter
    As before: redraw loop, its period, error lines, and all|running|done|alive
    with live a/r/d/h/q keys.
#>
param(
    [string]$Root = 'C:\Users\apoll\Desktop\vivado\probe',
    # Main-class projects: one row each, showing impl_1 once it has a log,
    # synth_1 before that. Point at the .runs directory.
    [string[]]$Main = @(
        'C:\Users\apoll\Desktop\vivado\multimesh_v7t\multimesh_v7t.runs',
        'C:\Users\apoll\Desktop\vivado\multimesh_v7\multimesh_v7.runs',
        'C:\Users\apoll\Desktop\vivado\multimesh_v67\multimesh_v67.runs',
        'C:\Users\apoll\Desktop\vivado\multimesh_v65\multimesh_v65.runs'
    ),
    [int]$TailLines = 600,
    [switch]$Watch,
    [int]$Every = 15,
    [switch]$Errors,
    [ValidateSet('all', 'running', 'done', 'alive')]
    [string]$Filter = 'all'
)

$script:Mode = $Filter
$Keys = @{ 'a' = 'all'; 'r' = 'running'; 'd' = 'done'; 'h' = 'alive' }
$SLOTS = 6

# path -> @{ tick; len; data } so an unchanged file is never re-read.
$script:Cache = @{}
# tag -> last shown WNS, for the movement arrow.
$script:Prev = @{}

$Fail = @('Design is not routable', 'IO Placement failed',
          'failed due to earlier errors', 'Error in initialization of Rule object',
          'not the BD wrapper', 'Mig 66-119', "can't read ""rt::result""")

function Test-Row {
    param($r)
    switch ($script:Mode) {
        'running' { $r.state -in @('running', 'retry') }
        'done'    { $r.state -in @('running', 'retry', 'done') }
        'alive'   { $r.state -notin @('dead', 'failed') }
        default   { $true }
    }
}

function Get-Tail {
    <# Cached tail of a log. Get-Content -Tail seeks from the end, so this is
       cheap even on a 300 MB runme.log. #>
    param([string]$Path, [int]$N = $TailLines)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $fi = Get-Item -LiteralPath $Path
    $key = "$Path|$N"
    $c = $script:Cache[$key]
    if ($c -and $c.tick -eq $fi.LastWriteTimeUtc.Ticks -and $c.len -eq $fi.Length) {
        return $c
    }
    $c = @{ tick = $fi.LastWriteTimeUtc.Ticks; len = $fi.Length
            mtime = $fi.LastWriteTime
            lines = @(Get-Content -LiteralPath $Path -Tail $N -ErrorAction SilentlyContinue) }
    $script:Cache[$key] = $c
    return $c
}

function Parse-Impl {
    <# Everything the display needs from ONE tail pass of an impl runme.log. #>
    param($tail)
    $r = @{ cmd = ''; phase = ''; wns = ''; tns = ''; whs = ''; ths = ''; cong = '' }
    if (-not $tail) { return $r }
    foreach ($ln in $tail.lines) {
        if ($ln -match '^Command: (\w+)') { $r.cmd = $Matches[1]; $r.phase = '' }
        elseif ($ln -match '^(Phase [\d.]+ [^|]+?)\s*$') { $r.phase = $Matches[1] }
        elseif ($ln -match 'WNS=\s*(-?[\d.]+)') {
            $r.wns = $Matches[1]
            if ($ln -match 'TNS=\s*(-?[\d.]+)') { $r.tns = '{0:n0}' -f [double]$Matches[1] }
            if ($ln -match 'WHS=\s*(-?[\d.]+)') { $r.whs = $Matches[1] }
            if ($ln -match 'THS=\s*(-?[\d.]+)') { $r.ths = '{0:n0}' -f [double]$Matches[1] }
        }
        elseif ($ln -match 'congestion is level (\d+) \((\d+x\d+)\)') {
            $r.cong = 'L{0} {1}' -f $Matches[1], $Matches[2]
        }
    }
    return $r
}

function Find-Error {
    param($tail)
    if (-not $tail) { return $null }
    foreach ($ln in $tail.lines) {
        foreach ($f in $Fail) { if ($ln.Contains($f)) { return $ln.Trim() } }
    }
    return $null
}

function Get-ProbeStatus {
    param([System.IO.DirectoryInfo]$Dir)
    $t = $Dir.Name
    $st = [ordered]@{
        probe = $t; stage = 'pending'; step = ''; elapsed = ''; idle = ''
        idleMin = 0.0; wns = ''; dwns = ' '; tns = ''; whs = ''; ths = ''
        cong = ''; state = 'pending'; err = ''; memGB = ''
    }
    # Either naming scheme: mp_<tag>.runs (old harness) or multimesh_*.runs (v6p).
    $runs = @(Get-ChildItem $Dir.FullName -Directory -Filter '*.runs' -ErrorAction SilentlyContinue)[0]
    $runLog  = Join-Path $Dir.FullName 'run.log'
    $implTop = Join-Path $Dir.FullName 'impl.log'
    $gateTop = Join-Path $Dir.FullName 'levels.log'
    if (-not (Test-Path $runLog) -and -not $runs) { return [pscustomobject]$st }

    $implLog = $null
    $oocLog = $null
    if ($runs) {
        $implLog = @(Get-ChildItem (Join-Path $runs.FullName 'impl*\runme.log') -ErrorAction SilentlyContinue |
                     Sort-Object LastWriteTime -Descending)[0]
        # OOC children write their own runme.log while every top log is silent.
        $oocLog = @(Get-ChildItem (Join-Path $runs.FullName '*\runme.log') -ErrorAction SilentlyContinue |
                    Sort-Object LastWriteTime -Descending)[0]
    }

    # Newest activity across the logs that matter; that file's age is `idle`.
    $cands = @(@($runLog, $implTop, $gateTop) | Where-Object { Test-Path $_ })
    if ($implLog) { $cands += $implLog.FullName }
    if ($oocLog)  { $cands += $oocLog.FullName }
    $newest = @($cands) | ForEach-Object { Get-Item -LiteralPath $_ } |
              Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($newest) {
        $idle = New-TimeSpan -Start $newest.LastWriteTime -End (Get-Date)
        $st.idleMin = $idle.TotalMinutes
        $st.idle = if ($idle.TotalSeconds -lt 60) { '{0:n0}s' -f $idle.TotalSeconds }
                   elseif ($idle.TotalMinutes -lt 60) { '{0:n0}m' -f $idle.TotalMinutes }
                   else { '{0:n0}h{1:n0}m' -f [math]::Floor($idle.TotalHours), $idle.Minutes }
    }
    $oldest = @($cands) | ForEach-Object { Get-Item -LiteralPath $_ } |
              Sort-Object CreationTime | Select-Object -First 1
    if ($oldest) {
        $st.elapsed = '{0:h\hmm\m}' -f (New-TimeSpan -Start $oldest.CreationTime -End (Get-Date))
    }

    # Stage from what exists; phase and timing from the newest impl tail.
    $st.state = 'running'
    if ($implLog) {
        $p = Parse-Impl (Get-Tail $implLog.FullName)
        $st.stage = switch ($p.cmd) {
            'opt_design'      { 'opt' }      'place_design'   { 'place' }
            'phys_opt_design' { 'physopt' }  'route_design'   { 'route' }
            'write_bitstream' { 'bitstream' } default         { 'impl' }
        }
        $st.step = $p.phase -replace '^Phase ', ''
        $st.wns = $p.wns; $st.tns = $p.tns; $st.whs = $p.whs; $st.ths = $p.ths
        $st.cong = $p.cong
    } elseif ($oocLog -and $oocLog.Directory.Name -eq 'synth_1') {
        $st.stage = 'synth'
        $tl = Get-Tail $oocLog.FullName 80
        foreach ($ln in $tl.lines) {
            if ($ln -match '^(Starting .+?) :') { $st.step = $Matches[1] }
        }
    } elseif ($oocLog) {
        $st.stage = 'ooc'
        $st.step = 'per-IP: ' + ($oocLog.Directory.Name -replace "^mp_$t`_", '')
    } else { $st.stage = 'bd' }

    # Gate verdict goes in STEP only: its design WNS is hard-IP noise at synth
    # (m62_c4 gated -5.918 on the un-placed MIG, which routes to -0.03).
    if ($runs -and -not $implLog) {
        $gl = Get-Tail $gateTop 60
        if ($gl) {
            $g = ''
            foreach ($ln in $gl.lines) {
                if ($ln -match '^@@@ deep\(9\+\) (\d+)') { $g = "gate deep9+=$($Matches[1])" }
                if ($ln -match '^@@@ wns (-?[\d.]+)') { $g += " dwns=$($Matches[1])" }
            }
            if ($g) { $st.step = $g }
        }
    }

    # Done markers live at the END of whichever top log finished. A marker
    # OLDER than a live impl runme.log is a previous stage's, not this run's.
    foreach ($log in @($implTop, $runLog, $gateTop)) {
        $tl = Get-Tail $log 40
        if ($tl -and ($tl.lines -match "@@@ probe $t done|@@@ v6 done|route_design completed successfully")) {
            if ($implLog -and $implLog.LastWriteTime -gt $tl.mtime) { continue }
            $st.stage = 'done'; $st.state = 'done'; break
        }
    }
    if ($st.state -ne 'done') {
        foreach ($log in @($implTop, $runLog, $gateTop)) {
            $e = Find-Error (Get-Tail $log 120)
            if ($e) {
                $st.state = if ($st.idleMin -lt 5) { 'retry' } else { 'failed' }
                $st.err = if ($e.Length -gt 110) { $e.Substring(0, 110) + '...' } else { $e }
                break
            }
        }
    }

    # Movement arrow against the last refresh that had a number.
    if ($st.wns -ne '') {
        $prev = $script:Prev[$t]
        if ($null -ne $prev) {
            $d = [double]$st.wns - [double]$prev
            $st.dwns = if ($d -gt 0.005) { '+' } elseif ($d -lt -0.005) { '-' } else { '=' }
        }
        $script:Prev[$t] = $st.wns
    }
    [pscustomobject]$st
}

function Get-MainStatus {
    <# One row per main project: impl_1 once it has a log, synth_1 before. #>
    param([string]$RunsDir)
    if (-not $RunsDir -or -not (Test-Path $RunsDir)) { return $null }
    $implLog = Join-Path $RunsDir 'impl_1\runme.log'
    $synLog  = Join-Path $RunsDir 'synth_1\runme.log'
    $isImpl = Test-Path $implLog
    $log = if ($isImpl) { $implLog } else { $synLog }
    if (-not (Test-Path $log)) { return $null }
    $fi = Get-Item -LiteralPath $log
    $p = Parse-Impl (Get-Tail $log)
    $idle = (New-TimeSpan -Start $fi.LastWriteTime -End (Get-Date))
    $done = $isImpl -and ((Get-Tail $log 40).lines -match 'write_bitstream completed|route_design completed successfully')
    $synStep = ''
    if (-not $isImpl) {
        $sl = (Get-Tail $log 200).lines | Where-Object { $_ -match '^Start\s+\w' } | Select-Object -Last 1
        if ($sl) { $synStep = ($sl -replace '^Start\s+', '') }
    }
    $st = [pscustomobject][ordered]@{
        probe = 'MAIN:' + ((Split-Path $RunsDir -Leaf) -replace '\.runs$', '')
        stage = if ($done) { 'done' } elseif (-not $isImpl) { 'synth' } else { switch ($p.cmd) {
            'opt_design' { 'opt' } 'place_design' { 'place' }
            'phys_opt_design' { 'physopt' } 'route_design' { 'route' }
            'write_bitstream' { 'bitstream' } default { 'impl' } } }
        step = if ($isImpl) { $p.phase -replace '^Phase ', '' } else { $synStep }
        elapsed = '{0:h\hmm\m}' -f (New-TimeSpan -Start $fi.CreationTime -End (Get-Date))
        idle = if ($idle.TotalMinutes -lt 60) { '{0:n0}m' -f $idle.TotalMinutes }
               else { '{0:n0}h{1:n0}m' -f [math]::Floor($idle.TotalHours), $idle.Minutes }
        idleMin = $idle.TotalMinutes
        wns = $p.wns; dwns = ' '; tns = $p.tns; whs = $p.whs; ths = $p.ths
        cong = $p.cong
        state = if ($done) { 'done' } elseif ($idle.TotalMinutes -ge 30) { 'dead' } else { 'running' }
        err = ''; memGB = ''
    }
    if ($st.wns -ne '') {
        $prev = $script:Prev[$st.probe]
        if ($null -ne $prev) {
            $d = [double]$st.wns - [double]$prev
            $st.dwns = if ($d -gt 0.005) { '+' } elseif ($d -lt -0.005) { '-' } else { '=' }
        }
        $script:Prev[$st.probe] = $st.wns
    }
    return $st
}

function Show-Status {
    $probes = Get-ChildItem -LiteralPath $Root -Directory -ErrorAction SilentlyContinue |
              Where-Object { $_.Name -notlike '*.dead' -and $_.Name -notlike '*.obsolete' } |
              Sort-Object Name
    # One process query per refresh: liveness AND per-run memory come from it.
    $procs = @(Get-CimInstance Win32_Process -Filter "Name='vivado.exe'" |
               Select-Object ProcessId, CommandLine, WorkingSetSize)

    $rows = @()
    foreach ($p in $probes) {
        $r = Get-ProbeStatus -Dir $p
        $mine = @($procs | Where-Object { $_.CommandLine -and $_.CommandLine -match [regex]::Escape($p.Name) })

        # An ad-hoc session (RQS, replication, census, ...) working ON this
        # probe gets its own row, named by its -log file; its memory leaves
        # the probe's own sum so the totals stay honest.
        $aux = @()
        foreach ($ap in $mine) {
            if ($ap.CommandLine -notmatch '-log\s+"?([^\s"]+\.log)"?') { continue }
            $lf = $Matches[1]
            if (-not [System.IO.Path]::IsPathRooted($lf)) { $lf = Join-Path $p.FullName $lf }
            if ($lf -match '\.runs\\') { continue }
            if (-not (Test-Path $lf)) { continue }
            $aux += $ap
            $afi = Get-Item -LiteralPath $lf
            $ap2 = Parse-Impl (Get-Tail $lf)
            $aidle = New-TimeSpan -Start $afi.LastWriteTime -End (Get-Date)
            $rows += [pscustomobject][ordered]@{
                probe = $p.Name + ':' + [System.IO.Path]::GetFileNameWithoutExtension($lf)
                stage = switch ($ap2.cmd) {
                    'place_design' { 'place' } 'phys_opt_design' { 'physopt' }
                    'route_design' { 'route' } default { 'session' } }
                step = $ap2.phase -replace '^Phase ', ''
                elapsed = '{0:h\hmm\m}' -f (New-TimeSpan -Start $afi.CreationTime -End (Get-Date))
                idle = '{0:n0}m' -f $aidle.TotalMinutes
                idleMin = $aidle.TotalMinutes
                wns = $ap2.wns; dwns = ' '; tns = $ap2.tns; whs = $ap2.whs; ths = $ap2.ths
                cong = $ap2.cong
                state = 'running'
                err = ''
                memGB = '{0:n1}' -f ($ap.WorkingSetSize / 1GB)
            }
        }
        $mine = @($mine | Where-Object { $_.ProcessId -notin @($aux | ForEach-Object ProcessId) })

        if ($mine.Count) {
            $r.memGB = '{0:n1}' -f (($mine | Measure-Object WorkingSetSize -Sum).Sum / 1GB)
            # A live process outranks a stale done-marker from an earlier stage.
            if ($r.state -eq 'done') { $r.state = 'running'; $r.stage = 'reopening' }
        } elseif ($r.state -ne 'done' -and $r.stage -notin @('pending', 'bd')) {
            $r.state = if ($r.idleMin -ge 5) { 'dead' } else { 'retry' }
        }
        $rows += $r
    }
    foreach ($md in $Main) {
        $m = Get-MainStatus -RunsDir $md
        if ($m) {
            # Same liveness rule as probe rows: a running Vivado holding the
            # project outranks a quiet log. Route Global Iterations go 45m+
            # without a log line while fully computing.
            $proj = (Split-Path $md -Leaf) -replace '\.runs$', ''
            $mine = @($procs | Where-Object { $_.CommandLine -and $_.CommandLine -match [regex]::Escape($proj) })
            if ($mine.Count) {
                $m.state = if ($m.state -eq 'done') { 'done' } else { 'running' }
                $m.memGB = '{0:n1}' -f (($mine | Measure-Object WorkingSetSize -Sum).Sum / 1GB)
            }
            $rows = @($m) + $rows
        }
    }

    # Deterministic: state rank, then name. No timestamp interleaving.
    $rank = @{ running = 0; retry = 1; pending = 2; done = 3; dead = 4; failed = 4 }
    $rows = $rows | Sort-Object @{ e = { $rank[[string]$_.state] } }, @{ e = { $_.probe } }
    $allRows = @($rows)
    $rows = @($rows | Where-Object { Test-Row $_ })

    $out = [System.Collections.Generic.List[object]]::new()
    $seg = { param($t, $c) @{ t = $t; c = $c } }
    # Name column sized to the longest visible run name, floor 14.
    $nw = 14
    foreach ($r in $rows) { if ($r.probe.Length -gt $nw) { $nw = $r.probe.Length } }
    $out.Add(@((& $seg ("{0,-$nw} {1,-9} {2,-7} {3,-6} {4,-9} {5,-11} {6,-7} {7,-9} {8,-10} {9,-6} {10}" -f `
        'run', 'stage', 'elapsed', 'idle', 'wns', 'tns', 'whs', 'congest', 'mem(GB)', 'd', 'phase') 'DarkGray')))
    foreach ($r in $rows) {
        $color = switch ($r.state) {
            'done' { 'Green' } 'failed' { 'Red' } 'dead' { 'Red' }
            'retry' { 'Magenta' } 'running' { 'Yellow' } default { 'DarkGray' } }
        $idleColor = if ($r.state -eq 'running' -and $r.idleMin -ge 10) { 'Red' } else { $color }
        $cc = if ($r.cong -match '^L([5-9])') { 'Red' } else { $color }
        $dc = switch ($r.dwns) { '+' { 'Green' } '-' { 'Red' } default { $color } }
        if ($r.state -eq 'dead') { $r.step = "NO PROCESS - $($r.step)" }
        $out.Add(@(
            (& $seg ("{0,-$nw} {1,-9} {2,-7} " -f $r.probe, $r.stage, $r.elapsed) $color),
            (& $seg ('{0,-6} ' -f $r.idle) $idleColor),
            (& $seg ('{0,-9} {1,-11} {2,-7} ' -f $r.wns, $r.tns, $r.whs) $color),
            (& $seg ('{0,-9} ' -f $r.cong) $cc),
            (& $seg ('{0,-10} ' -f $r.memGB) $color),
            (& $seg ('{0,-6} ' -f $r.dwns) $dc),
            (& $seg ('{0}' -f $r.step) $color)))
        if ($Errors -and $r.err) {
            $out.Add(@((& $seg ("            {0}" -f $r.err) 'DarkRed')))
        }
    }

    $gb   = [math]::Round((($procs | Measure-Object WorkingSetSize -Sum).Sum) / 1GB, 1)
    $free = [math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1MB, 0)
    $live = @($allRows | Where-Object { $_.state -in @('running', 'retry') }).Count
    $slotc = if ($live -ge $SLOTS) { 'Red' } elseif ($live -ge $SLOTS - 1) { 'Yellow' } else { 'Green' }
    $shown = if ($rows.Count -eq $allRows.Count) { '' }
             else { "  [{0}: {1} of {2}]" -f $script:Mode, $rows.Count, $allRows.Count }
    $out.Add(@((& $seg '' 'DarkGray')))
    $out.Add(@(
        (& $seg ("slots {0}/{1}" -f $live, $SLOTS) $slotc),
        (& $seg $shown 'Cyan'),
        (& $seg ("   {0} vivado, {1} GB resident, {2} GB free   {3:HH:mm:ss}" -f `
            $procs.Count, $gb, $free, (Get-Date)) 'DarkGray')))
    return $out
}

function Write-Lines($lines) {
    foreach ($ln in $lines) {
        for ($i = 0; $i -lt $ln.Count; $i++) {
            Write-Host $ln[$i].t -ForegroundColor $ln[$i].c -NoNewline:($i -lt $ln.Count - 1)
        }
    }
}

function Wait-Key {
    param([int]$Seconds)
    $until = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $until) {
        if (-not [Console]::IsInputRedirected -and [Console]::KeyAvailable) {
            $k = [string][Console]::ReadKey($true).KeyChar
            if ($k -eq 'q') { return 'quit' }
            if ($Keys.ContainsKey($k)) { $script:Mode = $Keys[$k]; return 'redraw' }
        }
        Start-Sleep -Milliseconds 120
    }
    return 'tick'
}

if ($Watch) {
    while ($true) {
        $lines = Show-Status
        Clear-Host
        Write-Lines $lines
        Write-Host ("a all  r running  d +done  h hide dead  q quit   [{0}]  every {1}s" `
                    -f $script:Mode, $Every) -ForegroundColor DarkGray
        if ((Wait-Key $Every) -eq 'quit') { break }
    }
}
else { Write-Lines (Show-Status) }
