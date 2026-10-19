Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = folder
pyw = folder & "\runtime\pythonw.exe"
If Not fso.FileExists(pyw) Then
    pyw = folder & "\.venv\Scripts\pythonw.exe"
End If
shell.Run """" & pyw & """ """ & folder & "\desktop.py""", 0, False
