Option Explicit
Dim sh, fso, root, ps, cmd
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = root
ps = sh.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\WindowsPowerShell\v1.0\powershell.exe"
cmd = Chr(34) & ps & Chr(34) & " -NoProfile -ExecutionPolicy Bypass -File " & Chr(34) & root & "\START.ps1" & Chr(34)
'Keep the PowerShell launcher hidden; startup diagnostics are written to data\launch.log.
sh.Run cmd, 0, False
