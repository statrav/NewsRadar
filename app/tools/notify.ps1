# 정리 결과 알림 (run.ps1이 부른다). 누르면 정리 페이지가 열린다 (실패·연결 풀림이면 페이지 맨 위에 이유가 보임)
# -Code: radar.py run의 종료 코드 (0 성공, 3 Claude 연결 풀림, 그 밖 실패)
param([int]$Code = 1)
$ErrorActionPreference = 'Continue'
$data = if ($env:NEWSRADAR_DATA) { $env:NEWSRADAR_DATA } else { Join-Path $env:LOCALAPPDATA 'NewsRadar\data' }
$today = (Get-Date).ToString('yyyy-MM-dd')
$page = ([Uri](Join-Path $data 'site\index.html')).AbsoluteUri
$log = Join-Path $data "logs\$today.log"

$archive = Join-Path $data "archive\$today.json"
if ($Code -eq 0 -and (Test-Path $archive)) {
    $day = Get-Content $archive -Raw -Encoding UTF8 | ConvertFrom-Json
    $names = @($day.themes | ForEach-Object { $_.name })
    $title = "오늘의 흐름 $($names.Count)개"
    $body = ($names | Select-Object -First 3) -join ' · '
    $url = $page
} elseif ($Code -eq 3) {
    $title = 'Claude 연결이 풀렸어요'
    $body = "바탕화면의 'NewsRadar 설정'을 열어 다시 연결해 주세요."
    $url = $page
} else {
    $title = '오늘 정리를 만들지 못했어요'
    $body = '눌러서 이유를 확인하세요. 내일 정한 시간에 다시 해 볼게요.'
    $url = if (Test-Path (Join-Path $data 'site\index.html')) { $page } elseif (Test-Path $log) { ([Uri]$log).AbsoluteUri } else { $page }
}

function Esc([string]$s) { [System.Security.SecurityElement]::Escape($s) }
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = @"
<toast activationType="protocol" launch="$(Esc $url)" duration="long">
  <visual><binding template="ToastGeneric">
    <text>NewsRadar · $(Esc $title)</text>
    <text>$(Esc $body)</text>
  </binding></visual>
  <actions><action content="열기" activationType="protocol" arguments="$(Esc $url)"/></actions>
</toast>
"@
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
# 시작 메뉴 NewsRadar 바로가기(앱 ID 'NewsRadar', shortcuts.ps1)가 있으면 알림 보낸 쪽 이름이 NewsRadar로 보인다
$startLink = Join-Path ([Environment]::GetFolderPath('Programs')) 'NewsRadar.lnk'
$appId = if (Test-Path $startLink) { 'NewsRadar' } else { '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe' }
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show([Windows.UI.Notifications.ToastNotification]::new($doc))
