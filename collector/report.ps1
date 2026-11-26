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
# 허브 요약 워커 등 내부 실행은 보고하지 않는다 (재귀 차단) — report.sh와 같은 위치·같은 조건
if ($env:MADISON_SUPPRESS -eq "1") { Bye }
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

function Notify-Handoffs {
    # 새 pending 핸드오프 → Windows 토스트 알림 (WinRT — 추가 모듈 불필요). 실패 무해.
    try {
        $stamp = Join-Path $MadDir "notified-handoffs"
        $rows = Invoke-RestMethod -Uri "$($cfg.MADISON_URL)/api/handoffs?mine=1" -Headers $Headers -TimeoutSec 3
        if (-not $rows) { return }
        $seen = if (Test-Path $stamp) { @(Get-Content $stamp) } else { @() }
        foreach ($h in @($rows)) {
            if ("$($h.id)" -in $seen) { continue }
            try {
                [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
                $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
                    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
                $texts = $xml.GetElementsByTagName("text")
                $texts.Item(0).AppendChild($xml.CreateTextNode("MADISON 핸드오프 도착 — $($h.from_name) → $($h.repo)")) | Out-Null
                $texts.Item(1).AppendChild($xml.CreateTextNode("$($h.hf) $($h.summary) — /pickup으로 수령")) | Out-Null
                [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("MADISON").Show(
                    [Windows.UI.Notifications.ToastNotification]::new($xml))
            } catch {}
            Add-Content $stamp "$($h.id)"
        }
    } catch {}
}

if ($EventName -eq "__flush") {
    Notify-Handoffs
    if ($mutex.WaitOne(2000)) { try { Flush-Spool } finally { $mutex.ReleaseMutex() } }
    Bye
}

# ── 계정 식별 — collector/account.py(허브·macOS 수집기와 공용 규칙)의 PowerShell 이식 ──
# 계정은 에이전트 env(CLAUDE_CONFIG_DIR·ANTHROPIC_*·CODEX_HOME)에서만 알 수 있어 훅 프로세스 안에서 구한다.
# 어떤 실패도 "" (계정 미상)이 되고, 미상이면 detail.account 키를 싣지 않는다. 인증 값(키·토큰)은 해시만 쓰고 보관하지 않는다.
$AccountMaxLen = 200
$CacheDir = Join-Path $MadDir "cache"

function Get-Sha256Hex([string]$text) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($text))
        return (($bytes | ForEach-Object { $_.ToString("x2") }) -join "")
    } finally { $sha.Dispose() }
}

function Get-Sha8([string]$text) { return (Get-Sha256Hex $text).Substring(0, 8) }

function Normalize-Account($value) {
    # 'api:<라벨>' 또는 소문자 이메일, 그 외는 ''
    if ($value -isnot [string]) { return "" }
    if ($value.StartsWith("api:")) {
        $rest = ($value.Substring(4).Trim() -replace '\s+', ' ')
        if (-not $rest) { return "" }
        $out = "api:" + $rest
    } else {
        $out = ($value -replace '\s+', '').ToLowerInvariant()
        if (-not $out.Contains("@")) { return "" }
    }
    return $out.Substring(0, [Math]::Min($AccountMaxLen, $out.Length))
}

function Get-Field($obj, [string]$name) {
    # JavaScriptSerializer(Dictionary<string,object>)와 ConvertFrom-Json -AsHashtable(Hashtable) 모두 IDictionary이고
    # 공개 메서드 ContainsKey를 가진다 (Dictionary의 IDictionary.Contains는 명시적 구현이라 PowerShell에 안 보인다)
    if ($obj -is [System.Collections.IDictionary] -and $obj.ContainsKey($name)) { return $obj[$name] }
    return $null
}

function Get-Text($obj, [string]$name) {
    $v = Get-Field $obj $name
    if ($v -is [string]) { return $v }
    return ""
}

function Import-WebExtensions {
    # Add-Type은 호출마다 어셈블리를 다시 찾아 PS 5.1 훅 지연이 커진다. 타입이 이미 있으면 건너뛴다.
    if (-not ("System.Web.Script.Serialization.JavaScriptSerializer" -as [type])) {
        Add-Type -AssemblyName System.Web.Extensions
    }
}

function Read-JsonFile([string]$path) {
    # .claude.json은 프로젝트 이력으로 수 MB가 될 수 있다. PS 5.1의 ConvertFrom-Json은 큰 입력에 느리고 한도가 있어
    # JavaScriptSerializer(MaxJsonLength=int.MaxValue)를 쓴다. 7+는 ConvertFrom-Json -AsHashtable.
    $text = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
    if ($PSVersionTable.PSVersion.Major -ge 7) {
        return ($text | ConvertFrom-Json -AsHashtable)
    }
    Import-WebExtensions
    $serializer = New-Object System.Web.Script.Serialization.JavaScriptSerializer
    $serializer.MaxJsonLength = [int]::MaxValue
    $serializer.RecursionLimit = 256
    return $serializer.DeserializeObject($text)
}

function Get-ClaudeLogin([string]$path) {
    # 로그인 파일에서 필요한 부분만 뽑는다. 파일이 크므로 (수정 시각, 크기)가 같으면 캐시를 쓴다.
    # 캐시에는 거절된 키 꼬리의 해시만 들어간다.
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    $item = Get-Item -LiteralPath $path
    $stamp = "$($item.LastWriteTimeUtc.Ticks):$($item.Length)"
    $cacheFile = Join-Path $CacheDir ("claude-login-" + (Get-Sha256Hex ([System.IO.Path]::GetFullPath($path))).Substring(0, 12) + ".json")
    try {
        if (Test-Path -LiteralPath $cacheFile) {
            $cached = Get-Content -LiteralPath $cacheFile -Raw | ConvertFrom-Json
            if ($cached.version -eq 2 -and "$($cached.stamp)" -eq $stamp) {
                return @{ email = "$($cached.email)"; organizationType = "$($cached.organizationType)"
                          organizationName = "$($cached.organizationName)"; billingType = "$($cached.billingType)"; rejected = @($cached.rejected | ForEach-Object { "$_" }) }
            }
        }
    } catch {}
    $obj = Read-JsonFile $path
    if ($obj -isnot [System.Collections.IDictionary]) { return $null }
    $oauth = Get-Field $obj "oauthAccount"
    $rejected = @()
    $responses = Get-Field $obj "customApiKeyResponses"
    $list = Get-Field $responses "rejected"
    if ($list -is [System.Collections.IEnumerable] -and $list -isnot [string]) {
        $rejected = @($list | Where-Object { $_ -is [string] } | ForEach-Object { Get-Sha256Hex $_ } | Sort-Object -Unique)
    }
    $data = @{ email = (Get-Text $oauth "emailAddress"); organizationType = (Get-Text $oauth "organizationType")
               organizationName = (Get-Text $oauth "organizationName"); billingType = (Get-Text $oauth "billingType"); rejected = $rejected }
    try {
        New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
        $tmp = "$cacheFile.$PID.tmp"
        (@{ version = 2; stamp = $stamp; email = $data.email; organizationType = $data.organizationType
            organizationName = $data.organizationName; billingType = $data.billingType; rejected = $rejected } | ConvertTo-Json -Compress) |
            Set-Content -LiteralPath $tmp -Encoding UTF8
        Move-Item -LiteralPath $tmp -Destination $cacheFile -Force
    } catch {}
    return $data
}

function Resolve-ClaudeAccount([bool]$headless) {
    foreach ($flag in @(@("CLAUDE_CODE_USE_BEDROCK", "api:bedrock"), @("CLAUDE_CODE_USE_VERTEX", "api:vertex"), @("CLAUDE_CODE_USE_FOUNDRY", "api:foundry"))) {
        $v = [Environment]::GetEnvironmentVariable($flag[0])
        if ($v -and @("1", "true", "yes", "on") -contains $v.Trim().ToLowerInvariant()) { return $flag[1] }
    }
    $token = [Environment]::GetEnvironmentVariable("ANTHROPIC_AUTH_TOKEN")
    if ($token -and $token.Trim()) { return "api:" + (Get-Sha8 $token) }   # AUTH_TOKEN이 API_KEY보다 우선

    $configDir = [Environment]::GetEnvironmentVariable("CLAUDE_CONFIG_DIR")
    $base = if ($configDir) { $configDir } else { $env:USERPROFILE }
    $loginPath = Join-Path $base ".claude.json"
    $key = [Environment]::GetEnvironmentVariable("ANTHROPIC_API_KEY")
    if ($key -and $key.Trim()) {
        # 대화형 프롬프트에서 거절한 키는 쓰이지 않는다. headless는 프롬프트가 없어 키를 그대로 쓴다.
        if ($headless) { return "api:" + (Get-Sha8 $key) }
        $login = Get-ClaudeLogin $loginPath
        $tail = $key.Substring([Math]::Max(0, $key.Length - 20))
        if ($null -eq $login -or ($login.rejected -notcontains (Get-Sha256Hex $tail))) { return "api:" + (Get-Sha8 $key) }
    }
    $login = Get-ClaudeLogin $loginPath
    if ($null -eq $login) { return "" }
    $orgName = $login.organizationName.Trim()
    $apiAccount = if ($orgName) { "api:" + $orgName } else { "" }
    # 1. claude_* -> 구독  2. api*(api_team, api_individual …) -> API
    # 3. 비었거나 처음 보는 조직 유형 -> billingType으로 판정(새 구독 유형을 API로 오판하지 않는다).
    #    둘 다 비어 있으면 예전 Claude Code가 쓰지 않던 경우라 구독으로 본다.
    $orgType = $login.organizationType.Trim().ToLowerInvariant()
    if ($orgType.StartsWith("claude_")) { return $login.email }
    if ($orgType.StartsWith("api")) { return $apiAccount }
    $billing = $login.billingType.Trim().ToLowerInvariant()
    if ((-not $billing) -or $billing.Contains("subscription")) { return $login.email }
    return $apiAccount
}

function Get-JwtClaims($token) {
    if ($token -isnot [string]) { return $null }
    $parts = $token.Split(".")
    if ($parts.Length -lt 2) { return $null }
    $segment = $parts[1].Replace("-", "+").Replace("_", "/")   # base64url → base64
    $segment += "=" * ((4 - $segment.Length % 4) % 4)
    $json = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($segment))
    if ($PSVersionTable.PSVersion.Major -ge 7) { return ($json | ConvertFrom-Json -AsHashtable) }
    Import-WebExtensions
    return (New-Object System.Web.Script.Serialization.JavaScriptSerializer).DeserializeObject($json)
}

function Resolve-CodexAccount {
    $codexHome = [Environment]::GetEnvironmentVariable("CODEX_HOME")
    $base = if ($codexHome) { $codexHome } else { Join-Path $env:USERPROFILE ".codex" }
    $authPath = Join-Path $base "auth.json"
    if (-not (Test-Path -LiteralPath $authPath -PathType Leaf)) { return "" }
    $auth = Read-JsonFile $authPath
    if ($auth -isnot [System.Collections.IDictionary]) { return "" }
    $apiKey = Get-Field $auth "OPENAI_API_KEY"
    if ($apiKey -isnot [string] -or -not $apiKey.Trim()) { $apiKey = "" }
    $tokens = Get-Field $auth "tokens"
    $hasTokens = ($tokens -is [System.Collections.IDictionary]) -and ($tokens.Count -gt 0)
    if ((Get-Field $auth "auth_mode") -eq "apikey" -or ($apiKey -and -not $hasTokens)) {
        if ($apiKey) { return "api:" + (Get-Sha8 $apiKey) }
        return ""
    }
    if ($tokens -isnot [System.Collections.IDictionary]) { return "" }
    $claims = Get-JwtClaims (Get-Field $tokens "id_token")
    if ($claims -isnot [System.Collections.IDictionary]) { return "" }
    $email = Get-Field $claims "email"
    if ($email -isnot [string] -or -not $email.Trim()) {
        $email = Get-Field (Get-Field $claims "https://api.openai.com/profile") "email"
    }
    if ($email -is [string]) { return $email }
    return ""
}

function Resolve-Account([string]$agent, [bool]$headless) {
    try {
        if ($agent -eq "claude-code") { return (Normalize-Account (Resolve-ClaudeAccount $headless)) }
        if ($agent -eq "codex-cli") { return (Normalize-Account (Resolve-CodexAccount)) }
    } catch {}
    return ""
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

# 세션 이름 — Claude: 전사본 꼬리의 custom-title(/rename·앱 제목), 없으면 ai-title(자동 제목).
# Codex: ~/.codex/session_index.jsonl의 thread_name. 없으면 안 싣는다(허브가 빈 값은 무시). report.sh와 동일 규칙.
$sessionName = ""
try {
    if ($AgentName -eq "claude-code" -and $transcriptPath -and (Test-Path $transcriptPath)) {
        $customTitle = ""; $aiTitle = ""
        foreach ($line in (Get-Content $transcriptPath -Tail 4000)) {
            if ($line -match '"type":\s*"custom-title"') {
                $t = "$(($line | ConvertFrom-Json).customTitle)"; if ($t) { $customTitle = $t }
            } elseif ($line -match '"type":\s*"ai-title"') {
                $t = "$(($line | ConvertFrom-Json).aiTitle)"; if ($t) { $aiTitle = $t }
            }
        }
        $sessionName = if ($customTitle) { $customTitle } else { $aiTitle }
    } elseif ($AgentName -eq "codex-cli") {
        $codexBase = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE ".codex" }
        $index = Join-Path $codexBase "session_index.jsonl"
        if (Test-Path $index) {
            $needle = '"id":\s*"' + [regex]::Escape($sid) + '"'
            foreach ($line in (Get-Content $index)) {
                if ($line -match $needle) { $t = "$(($line | ConvertFrom-Json).thread_name)"; if ($t) { $sessionName = $t } }
            }
        }
    }
} catch {}
$sessionName = Trunc ($sessionName -replace '[\r\n\t]+', ' ') 120

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
if ($sessionName) { $detail.session_name = $sessionName }
# 계정은 session_start·prompt·turn_done에만, 정해졌을 때만 싣는다 (report.sh와 같은 규칙; 허브는 빈 값을 무시)
if ($EventName -in @("session_start", "prompt", "turn_done")) {
    $account = Resolve-Account $AgentName ($frontend -eq "auto")
    if ($account) { $detail.account = $account }
}

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
            # 자동 안내는 상태 불변. 문서를 확인한 pickup이 delivered로 기록한다.
            Write-Output "[MADISON] $($h.from_name)발 핸드오프 $($h.hf) 대기 중: $($h.summary). pickup 스킬(/pickup)로 내용을 확인하고 수신 처리하세요. 후속 작업 착수는 사용자 지시에 따릅니다."
        }
    } catch {}
}

Bye
