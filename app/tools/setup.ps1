# 설정 화면 열기 (바탕화면 'NewsRadar 설정'이 부른다). 이 창을 닫으면 설정 화면도 닫힌다.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$host.UI.RawUI.WindowTitle = 'NewsRadar 설정 (이 창은 닫지 마세요. 끝나면 저절로 닫혀요)'
$bin = Join-Path $env:USERPROFILE '.local\bin'
if ($env:Path -notlike "*$bin*") { $env:Path = "$bin;$env:Path" }
$app = Split-Path -Parent $PSScriptRoot
& uv run --quiet (Join-Path $app 'radar.py') setup
