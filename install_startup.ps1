$ProjectRoot = $PSScriptRoot
$BatPath = Join-Path $ProjectRoot 'start_jarvis.bat'
$ws = New-Object -ComObject WScript.Shell
$startup = $ws.SpecialFolders('Startup')

$shortcut = $ws.CreateShortcut((Join-Path $startup 'JARVIS.lnk'))
$shortcut.TargetPath = 'cmd.exe'
$shortcut.Arguments = "/c `"$BatPath`""
$shortcut.WorkingDirectory = $ProjectRoot
$shortcut.Description = 'J.A.R.V.I.S. Voice Assistant'
$shortcut.WindowStyle = 1
$shortcut.Save()

Write-Host "Startup shortcut created at: $(Join-Path $startup 'JARVIS.lnk')"
