Option Explicit
Dim fso, sh, folder, command, i
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File " & Chr(34) & folder & "\launch.ps1" & Chr(34)
For i = 0 To WScript.Arguments.Count - 1
  If WScript.Arguments(i) = "--minimized" Then command = command & " -Minimized"
  If WScript.Arguments(i) = "--demo" Then command = command & " -Demo"
Next
sh.CurrentDirectory = folder
sh.Run command, 0, False
