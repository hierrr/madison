# MADISON Claude Code / Codex collector — Windows 베타 (IMPLEMENTATION.md §8.5) ※ 실기기 미검증
# 원칙은 report.sh와 동일: 어떤 경우에도 세션을 방해하지 않는다(항상 exit 0).
param([string]$EventName = "", [string]$AgentName = "claude-code")

$ErrorActionPreference = "SilentlyContinue"
$MadDir = Join-Path $env:USERPROFILE ".claude\madison"
$EnvFile = Join-Path $MadDir "env"
$Spool = Join-Path $MadDir "spool.jsonl"
$ThrottleDir = Join-Path $MadDir "throttle"

function Bye { exit 0 }
if (-not $EventName) { Bye }
if ($AgentName -notin @("claude-code", "codex-cli")) { Bye }
if (-not (Test-Path $EnvFile)) { Bye }

# env 파싱 (KEY=VALUE)
$cfg = @{}
Get-Content $EnvFile | ForEach-Object {
    if ($_ -match '^\s*([A-Z_]+)\s*=\s*(.*)$') { $cfg[$Matches[1]] = $Matches[2].Trim('"') }
}
if (-not $cfg.MADISON_URL -or -not $cfg.MADISON_TOKEN) { Bye }
$Headers = @{ Authorization = "Bearer $($cfg.MADISON_TOKEN)"; "Content-Type" = "application/json" }
New-Item -ItemType Directory -Force -Path $ThrottleDir | Out-Null

$mutex = New-Object System.Threading.Mutex($false, "MADISON_SPOOL")

function Post-Body([string]$json, [int]$timeout) {
    try {
        Invoke-RestMethod -Uri "$($cfg.MADISON_URL)/api/events" -Method Post -Headers $Headers `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec $timeout | Out-Null
        return $true
    } catch { return $false }
}

function Flush-Spool {
    while ((Test-Path $Spool) -and (Get-Item $Spool).Length -gt 0) {
        $lines = Get-Content $Spool -TotalCount 100
        $arr = "[" + (($lines | Where-Object { $_ }) -join ",") + "]"
        if (-not (Post-Body $arr 5)) { return }
        $all = Get-Content $Spool
        if ($all.Count -le $lines.Count) { Clear-Content $Spool }
        else { $all | Select-Object -Skip $lines.Count | Set-Content $Spool }
    }
}

if ($EventName -eq "__flush") {
    if ($mutex.WaitOne(2000)) { try { Flush-Spool } finally { $mutex.ReleaseMutex() } }
    Bye
}

$stdin = [Console]::In.ReadToEnd()
try { $hook = $stdin | ConvertFrom-Json } catch { Bye }
if ($cfg.MADISON_DEBUG -eq "1") {
    New-Item -ItemType Directory -Force -Path (Join-Path $MadDir "samples") | Out-Null
    $stdin | Set-Content (Join-Path $MadDir "samples\$EventName.json")
}

$sid = if ($hook.session_id) { $hook.session_id } else { "unknown" }
$cwd = if ($hook.cwd) { $hook.cwd } else { "" }
$transcriptPath = if ($hook.transcript_path) { "$($hook.transcript_path)" } else { "" }
$timeoutSec = if ($EventName -eq "session_start") { 1 } else { 2 }

# 스로틀 (세션당 60초)
if ($EventName -in @("heartbeat", "tool_start")) {
    $stamp = Join-Path $ThrottleDir $sid
    if ((Test-Path $stamp) -and ((Get-Date) - (Get-Item $stamp).LastWriteTime).TotalSeconds -lt 60) { Bye }
    New-Item -ItemType File -Force -Path $stamp | Out-Null
}
if ($EventName -eq "permission_request") { Remove-Item (Join-Path $ThrottleDir $sid) -Force }

# 실행 경로: Codex rollout originator 우선, Windows 프로세스 조상 폴백.
$frontend = ""
if ($AgentName -eq "codex-cli" -and $transcriptPath -and (Test-Path $transcriptPath)) {
    try {
        $meta = Get-Content $transcriptPath -TotalCount 1 | ConvertFrom-Json
        $originator = "$($meta.payload.originator)"
        $sessionSource = "$($meta.payload.source)"
        if ($originator -in @("codex_exec", "exec")) { $frontend = "auto" }
        elseif ($originator -in @("codex-tui", "cli")) { $frontend = "cli" }
        elseif ($originator -in @("Codex Desktop", "codex-desktop", "codex_desktop")) { $frontend = "app" }
        elseif ($originator -match '(?i)(vscode|visual studio code)') { $frontend = "ide" }
    } catch {}
}
if (-not $frontend) {
    try {
        $cursor = $PID
        for ($i = 0; $i -lt 8 -and $cursor -gt 0; $i++) {
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$cursor"
            $cmd = "$($proc.CommandLine)"
            $exe = "$($proc.ExecutablePath)"
            if ($exe -match '(?i)(Claude|Codex)\.exe$' -and $cmd -match '(?i)(desktop|app)') { $frontend = "app"; break }
            if ($AgentName -eq "codex-cli" -and ($exe -match '(?i)(Code|code-insiders)\.exe$' -or $cmd -match '(?i)\.vscode[\\/]extensions')) { $frontend = "ide"; break }
            if ($cmd -match '(?i)claude(.+)(--print|-p\s)' -or $cmd -match '(?i)codex\s+exec') { $frontend = "auto"; break }
            $cursor = [int]$proc.ParentProcessId
        }
    } catch {}
}
if (-not $frontend -and $AgentName -eq "codex-cli") {
    if ($sessionSource -eq "cli") { $frontend = "cli" }
    elseif ($sessionSource -eq "exec") { $frontend = "auto" }
    elseif ($sessionSource -eq "vscode") { $frontend = "ide" }
}
if (-not $frontend) { $frontend = if ($AgentName -eq "claude-code") { "cli" } else { "unknown" } }

$model = if ($hook.model) { "$($hook.model)" } else { "" }
$effort = ""
if ($hook.effort -is [string]) { $effort = "$($hook.effort)" }
elseif ($hook.effort.level) { $effort = "$($hook.effort.level)" }

function Trunc([string]$value, [int]$max) {
    if (-not $value) { return "" }
    return $value.Substring(0, [Math]::Min($max, $value.Length))
}

# 프로젝트/브랜치
$project = ""; $branch = ""; $origin = ""
if ($cwd -and (Test-Path $cwd)) {
    $top = git -C $cwd rev-parse --show-toplevel 2>$null
    if ($top) {
        $project = Split-Path $top -Leaf
        $branch = git -C $cwd branch --show-current 2>$null
        $origin = git -C $cwd remote get-url origin 2>$null
    } else { $project = Split-Path $cwd -Leaf }
}

$detail = @{}
switch ($EventName) {
    "session_start" { $detail = @{ source = "$($hook.source)" } }
    "prompt" { $detail = @{ prompt = Trunc "$($hook.prompt)" 600 } }
    "tool_start" { $detail = @{ tool = "$($hook.tool_name)" } }
    "permission_request" {
        $message = if ($hook.message) { "$($hook.message)" } elseif ($hook.title) { "$($hook.title)" } `
            elseif ($hook.tool_input.description) { "$($hook.tool_input.description)" } else { "$($hook.tool_name)" }
        $detail = @{ message = Trunc $message 200 }
    }
    "idle" { $detail = @{ message = Trunc "$($hook.message)" 200 } }
    "turn_done" { $detail = @{ summary = Trunc "$($hook.last_assistant_message)" 200 } }
    "session_end" { $detail = @{ reason = "$($hook.reason)" } }
}
$detail.frontend = $frontend
$detail.model = $model
$detail.effort = $effort
$detail.collection_mode = "hooks"

$eventId = [guid]::NewGuid().ToString()
if ($AgentName -eq "codex-cli" -and $hook.turn_id -and $EventName -in @("prompt", "turn_done")) {
    $eventId = "codex-$sid-$($hook.turn_id)-$EventName"
} elseif ($AgentName -eq "codex-cli" -and $hook.turn_id -and $hook.tool_use_id -and $EventName -in @("tool_start", "heartbeat")) {
    $eventId = "codex-$sid-$($hook.turn_id)-$EventName-$($hook.tool_use_id)"
}

$ev = @{
    agent = $AgentName; session_id = $sid; event = $EventName
    ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
    event_id = $eventId
    project = $project; branch = "$branch"; detail = $detail
} | ConvertTo-Json -Compress -Depth 6

# 전송 (순서 보존: 스풀이 있으면 뒤에 붙여 함께 플러시)
if ((Test-Path $Spool) -and (Get-Item $Spool).Length -gt 0) {
    if ($mutex.WaitOne(1000)) {
        try { Add-Content $Spool $ev; Flush-Spool } finally { $mutex.ReleaseMutex() }
    } else { Add-Content $Spool $ev }
} elseif (-not (Post-Body $ev $timeoutSec)) {
    if ($mutex.WaitOne(1000)) { try { Add-Content $Spool $ev } finally { $mutex.ReleaseMutex() } }
    else { Add-Content $Spool $ev }
}

# SessionStart: 핸드오프 주입 (source startup|resume, 리포 매칭)
if ($EventName -eq "session_start" -and "$($hook.source)" -in @("startup", "resume")) {
    try {
        $q = "mine=1&repo=$([uri]::EscapeDataString($project))&origin=$([uri]::EscapeDataString("$origin"))"
        $hs = Invoke-RestMethod -Uri "$($cfg.MADISON_URL)/api/handoffs?$q" -Headers $Headers -TimeoutSec 1
        foreach ($h in $hs) {
            Write-Output "[MADISON] $($h.from_name)발 핸드오프 $($h.hf) 도착: $($h.summary). 브랜치 $($h.branch)를 체크아웃하고 $($h.doc_path)를 읽은 뒤, 이어서 진행할지 사용자에게 확인하세요."
            Invoke-RestMethod -Uri "$($cfg.MADISON_URL)/api/handoffs/$($h.id)" -Method Patch -Headers $Headers `
                -Body '{"status":"delivered"}' -TimeoutSec 1 | Out-Null
        }
    } catch {}
}

Bye
