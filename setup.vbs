Set objShell = CreateObject("WScript.Shell")
' Run setup.bat in a visible command prompt window
objShell.Run "cmd.exe /c setup.bat", 1, True
