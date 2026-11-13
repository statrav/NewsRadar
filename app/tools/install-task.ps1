# 매일 정한 시각에 run.ps1을 실행하도록 작업 스케줄러에 등록 (설정 화면 [시작하기]와 업데이트 때 부른다)
# 끄기: install-task.ps1 -Remove
param([switch]$Remove, [string]$At = '09:30')
$name = 'NewsRadar'

if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    return
}

$script = Join-Path $PSScriptRoot 'run.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Daily -At $At -RandomDelay (New-TimeSpan -Minutes 10)  # 여러 사람이 같은 시각에 몰리지 않게
# PC가 꺼져 있었으면 켜진 뒤 실행, 배터리에서도 실행, 30분 넘으면 멈춤
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName $name -Description '매일 소식을 모아 정리하고 알림 (NewsRadar)' -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
$next = (Get-ScheduledTask -TaskName $name | Get-ScheduledTaskInfo).NextRunTime
"예약 완료. 다음 실행: $next"
