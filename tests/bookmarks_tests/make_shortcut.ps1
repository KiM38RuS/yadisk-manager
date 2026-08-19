param(
    [string]$TargetPath,
    [string]$ShortcutPath,
    [string]$Description
)
$wshell = New-Object -ComObject WScript.Shell
$shortcut = $wshell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = $TargetPath
$shortcut.Description = $Description
$shortcut.WorkingDirectory = [System.IO.Path]::GetDirectoryName($TargetPath)
$shortcut.Save()
