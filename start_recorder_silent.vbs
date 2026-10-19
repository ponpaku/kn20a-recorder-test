Set shell = CreateObject("WScript.Shell")
base = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
supervisor = base & "\supervisor.py"
command = "pyw -3 " & Chr(34) & supervisor & Chr(34)
shell.Run command, 0, False
