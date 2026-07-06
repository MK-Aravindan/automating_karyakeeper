Set objFSO = CreateObject("Scripting.FileSystemObject")
Set objShell = CreateObject("WScript.Shell")

' Get the exact folder where this VBS script is located
strScriptFolder = objFSO.GetParentFolderName(WScript.ScriptFullName)

' Change the working directory to that folder to prevent System32 errors
objShell.CurrentDirectory = strScriptFolder

' Run run.bat reliably from its absolute path
objShell.Run "cmd.exe /c """ & strScriptFolder & "\run.bat""", 1, True
