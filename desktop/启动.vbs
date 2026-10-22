Option Explicit
Dim fso, sh, folder, py, cmd, i
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
py = "C:\Program Files\Python312\pythonw.exe"
If Not fso.FileExists(py) Then
  MsgBox "Python 3.12 was not found: " & py, 16, "Codex Reset Card"
  WScript.Quit 1
End If
cmd = Chr(34) & py & Chr(34) & " " & Chr(34) & folder & "\reset_card_panel.py" & Chr(34)
For i = 0 To WScript.Arguments.Count - 1
  If WScript.Arguments(i) = "--minimized" Or WScript.Arguments(i) = "--demo" Then
    cmd = cmd & " " & WScript.Arguments(i)
  End If
Next
sh.CurrentDirectory = folder
sh.Run cmd, 0, False
