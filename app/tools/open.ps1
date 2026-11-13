# 바탕화면 'NewsRadar': 정리 페이지를 연다. 아직 없으면 설정 화면을 연다.
$data = if ($env:NEWSRADAR_DATA) { $env:NEWSRADAR_DATA } else { Join-Path $env:LOCALAPPDATA 'NewsRadar\data' }
$page = Join-Path $data 'site\index.html'
if (Test-Path $page) { Start-Process $page } else { & (Join-Path $PSScriptRoot 'setup.ps1') }
