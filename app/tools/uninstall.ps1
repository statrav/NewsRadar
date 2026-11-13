# NewsRadar 지우기 (제거.cmd가 부른다): 예약·바로가기·코드를 지우고, 내 설정과 정리 기록을 지울지 묻는다
Add-Type -AssemblyName System.Windows.Forms
$root = Join-Path $env:LOCALAPPDATA 'NewsRadar'
$app = Join-Path $root 'app'
$data = Join-Path $root 'data'

$ok = [System.Windows.Forms.MessageBox]::Show('NewsRadar를 지울까요?', 'NewsRadar 지우기', 'YesNo', 'Question')
if ($ok -ne 'Yes') { return }

Unregister-ScheduledTask -TaskName 'NewsRadar' -Confirm:$false -ErrorAction SilentlyContinue
& (Join-Path $PSScriptRoot 'shortcuts.ps1') -Remove   # 바탕화면·시작 메뉴

$keep = [System.Windows.Forms.MessageBox]::Show("내 설정과 지난 정리 기록, Claude 연결도 지울까요?`n'아니요'를 누르면 남겨 두고, 다시 설치하면 그대로 이어서 써요.", 'NewsRadar 지우기', 'YesNo', 'Question')
Remove-Item $app -Recurse -Force -ErrorAction SilentlyContinue
if ($keep -eq 'Yes') { Remove-Item $root -Recurse -Force -ErrorAction SilentlyContinue }
[System.Windows.Forms.MessageBox]::Show('지웠어요.', 'NewsRadar', 'OK', 'Information') | Out-Null
