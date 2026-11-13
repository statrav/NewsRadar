# 매일 실행 (작업 스케줄러가 부른다): 수집 → Claude 정리 → 페이지는 radar.py run이 하고, 끝나면 알림
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8   # 없으면 로그의 한글이 깨진다
$bin = Join-Path $env:USERPROFILE '.local\bin'
if ($env:Path -notlike "*$bin*") { $env:Path = "$bin;$env:Path" }
$app = Split-Path -Parent $PSScriptRoot
& uv run --quiet (Join-Path $app 'radar.py') run *> $null
$code = $LASTEXITCODE
& (Join-Path $PSScriptRoot 'notify.ps1') -Code $code
