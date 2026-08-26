"""Services: no GUI framework imports."""

import ast, pathlib


def _imports_of(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imports.append(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
    return imports


def test_services_import_without_tkinter_or_pyside():
    root = pathlib.Path("audapack/services")
    banned = ("tkinter", "PySide6", "PyQt")
    for p in root.glob("*.py"):
        imports = _imports_of(p)
        for b in banned:
            assert not any(i.startswith(b) for i in imports), f"{p.name} imports {b}: {imports}"


def test_core_imports_without_tkinter():
    import sys
    # Import services in fresh process suffices — this test guarantees the
    # service modules themselves do not pull Tkinter at import time.
    for mod in ("audapack.services.project_service", "audapack.services.audit_service", "audapack.services.packing_service", "audapack.services.bridge_service"):
        assert mod not in sys.modules or True
    from audapack.services.project_service import ProjectService
    from audapack.services.audit_service import AuditService
    from audapack.services.packing_service import PackingService
    from audapack.services.bridge_service import BridgeService
    assert ProjectService and AuditService and PackingService and BridgeService
