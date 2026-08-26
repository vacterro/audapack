# CLI & Silent Packaging Reference

## 💻 Command Line Interface

AUDAPACK provides a powerful CLI interface for scripting, headless batch packaging, and system administration.

```text
usage: AUDAPACK.pyw [-h] [--pack PATH] [--pack-project ID] [--silent]
                    [--install-context-menu] [--remove-context-menu]
                    [--status] [--bridge]
```

---

## 📋 Arguments & Commands

### 1. Silent Full Packaging (`--silent`)
Packs all enabled projects in the registry without showing any graphical user interface:
```cmd
pythonw AUDAPACK.pyw --silent
```
*Creates timestamped `.zip` archives in each project's configured output directory.*

### 2. Pack Single Project (`--pack-project <ID>`)
Packs a specific project by its registry identifier:
```cmd
python AUDAPACK.pyw --pack-project AUDAPACK
```

### 3. Pack Arbitrary Path (`--pack <PATH>`)
Creates an atomic `.zip` package from any given file or folder path:
```cmd
python AUDAPACK.pyw --pack "C:\MyProject"
```

### 4. Status Inspection (`--status`)
Outputs project registry details, audit readiness, and freshness temperatures to `stdout`:
```cmd
python AUDAPACK.pyw --status
```

### 5. Windows Explorer Context Menu
Install or remove the right-click integration (*"Упаковать через AUDAPACK"*):
```cmd
# Install integration into HKCU
python AUDAPACK.pyw --install-context-menu

# Remove integration from HKCU
python AUDAPACK.pyw --remove-context-menu
```

---

## 🔒 Atomic Packaging Mechanics

1. Target directory is traversed and filtered against mandatory excludes (`__pycache__`, `.git`, `.venv`, `node_modules`, `.part`, `.zip`).
2. Archive is written to a temporary `.part` file (e.g. `_AUDAPACK_2026-08-27_015500.zip.part`).
3. Integrity is verified using `zipfile.testzip()` to guarantee complete CRC check.
4. `.part` file is renamed to the final `.zip` file atomically.
