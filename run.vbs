Set objShell = CreateObject("WScript.Shell")
' Run run.bat in a visible command prompt window
objShell.Run "cmd.exe /c run.bat", 1, True
