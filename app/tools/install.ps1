# NewsRadar 설치·업데이트 (설치.cmd가 부른다)
# 1) uv·Claude Code가 없으면 사용자 폴더에 설치 (관리자 권한 필요 없음)
# 2) 코드를 %LOCALAPPDATA%\NewsRadar\app 으로 복사 (data는 그대로 둔다)
# 3) 바탕화면 바로가기 2개
# 4) 처음이면 설정 화면을 연다. 이미 설정했으면(업데이트) 예약만 다시 걸고 끝낸다

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms

$src = Split-Path -Parent $PSScriptRoot              # 압축을 푼 곳의 app 폴더
$root = Join-Path $env:LOCALAPPDATA 'NewsRadar'
$app = Join-Path $root 'app'
$data = Join-Path $root 'data'
$bin = Join-Path $env:USERPROFILE '.local\bin'

function Say([string]$msg) { Write-Host "  $msg" }
function Fail([string]$msg) {
    [System.Windows.Forms.MessageBox]::Show($msg, 'NewsRadar 설치', 'OK', 'Error') | Out-Null
    exit 1
}
function Has([string]$cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function AddBinToPath {
    if ((Test-Path $bin) -and ($env:Path -notlike "*$bin*")) { $env:Path = "$bin;$env:Path" }
}

Write-Host ''
Write-Host 'NewsRadar 설치를 시작해요. 이 창은 끝나면 저절로 닫혀요.'
Write-Host ''
AddBinToPath

try {
    if (-not (Has 'uv')) {
        Say '필요한 프로그램(uv)을 설치하고 있어요...'
        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex" | Out-Null
        AddBinToPath
        if (-not (Has 'uv')) { Fail "필요한 프로그램(uv)을 설치하지 못했어요.`n인터넷 연결을 확인하고 설치.cmd를 다시 실행해 주세요." }
    }
    if (-not (Has 'claude')) {
        Say 'Claude Code를 설치하고 있어요... (1~2분)'
        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex" | Out-Null
        AddBinToPath
        if (-not (Has 'claude')) { Fail "Claude Code를 설치하지 못했어요.`n인터넷 연결을 확인하고 설치.cmd를 다시 실행해 주세요." }
    }

    Say '파일을 복사하고 있어요...'
    New-Item -ItemType Directory -Force -Path $app, $data | Out-Null
    robocopy $src $app /MIR /NFL /NDL /NJH /NJS /NP /XD '__pycache__' | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail '파일을 복사하지 못했어요. 설치.cmd를 다시 실행해 주세요.' }

    Say '처음 한 번 준비하고 있어요... (1분쯤)'
    & uv run --quiet (Join-Path $app 'radar.py') where | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail '준비하다가 문제가 생겼어요. 인터넷 연결을 확인하고 설치.cmd를 다시 실행해 주세요.' }

    & (Join-Path $app 'tools\shortcuts.ps1')
} catch {
    Fail "설치하다가 문제가 생겼어요.`n$($_.Exception.Message)"
}

$profileFile = Join-Path $data 'profile.yaml'
if (Test-Path (Join-Path $data 'sources.yaml')) {
    # 업데이트: 설정은 그대로, 예약만 새 코드로 다시 건다
    $time = (Select-String -Path $profileFile -Pattern "^time:\s*['""]?(\d{1,2}:\d{2})" | Select-Object -First 1).Matches.Groups[1].Value
    if (-not $time) { $time = '09:30' }
    & (Join-Path $app 'tools\install-task.ps1') -At $time | Out-Null
    [System.Windows.Forms.MessageBox]::Show("업데이트했어요. 설정은 그대로예요.`n매일 $time 에 정리해요.", 'NewsRadar', 'OK', 'Information') | Out-Null
} else {
    Say '설정 화면을 열어요. 브라우저에서 이어서 해 주세요.'
    Start-Process powershell.exe -WindowStyle Minimized -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $app 'tools\setup.ps1'))
}
