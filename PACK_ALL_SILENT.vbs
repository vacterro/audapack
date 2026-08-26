Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
pywScript = fso.BuildPath(scriptDir, "AUDAPACK.pyw")

shell.CurrentDirectory = scriptDir
shell.Run "pythonw """ & pywScript & """ --silent", 0, False
