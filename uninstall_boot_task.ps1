#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"

$taskName = "KN20A Recorder"
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Output "Removed: $taskName"
} else {
    Write-Output "Not registered: $taskName"
}
