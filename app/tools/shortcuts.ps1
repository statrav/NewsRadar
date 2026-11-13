# 바탕화면 바로가기 2개: NewsRadar(오늘 정리 보기), NewsRadar 설정(설정 화면 다시 열기)
param([switch]$Remove)
$desktop = [Environment]::GetFolderPath('Desktop')
$links = @{
    'NewsRadar.lnk'      = @{ File = 'open.ps1';  Style = 7; Desc = '오늘의 정리 보기' }
    'NewsRadar 설정.lnk' = @{ File = 'setup.ps1'; Style = 7; Desc = '분야·받을 시각 바꾸기, Claude 다시 연결' }
}
if ($Remove) {
    foreach ($name in $links.Keys) { Remove-Item (Join-Path $desktop $name) -ErrorAction SilentlyContinue }
    return
}
$shell = New-Object -ComObject WScript.Shell
foreach ($name in $links.Keys) {
    $l = $links[$name]
    $s = $shell.CreateShortcut((Join-Path $desktop $name))
    $s.TargetPath = 'powershell.exe'
    $s.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $PSScriptRoot $l.File)`""
    $s.WindowStyle = $l.Style   # 7 = 최소화된 창
    $s.Description = $l.Desc
    $s.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
    $s.Save()
}
