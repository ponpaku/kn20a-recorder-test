#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"

$taskName = "KN20A Recorder"
$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$statusPath = Join-Path $appDir "data\boot_task_status.json"
New-Item -ItemType Directory -Path (Split-Path -Parent $statusPath) -Force | Out-Null
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue

if ($null -eq $task) {
    throw "Scheduled task not found: $taskName"
}

Disable-ScheduledTask -TaskName $taskName | Out-Null
$task = Get-ScheduledTask -TaskName $taskName
$taskInfo = Get-ScheduledTaskInfo -TaskName $taskName

[ordered]@{
    updated_at = (Get-Date).ToString("o")
    task_name = $taskName
    task_state = [string]$task.State
    task_enabled = [bool]$task.Settings.Enabled
    last_task_result = $taskInfo.LastTaskResult
    note = "Automatic startup disabled; an already-running process is not stopped."
} | ConvertTo-Json | Set-Content -LiteralPath $statusPath -Encoding UTF8

Write-Output "Disabled: $taskName"
Write-Output "Enabled: $($task.Settings.Enabled)"
Write-Output "State: $($task.State)"
