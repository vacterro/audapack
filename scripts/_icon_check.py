"""Icon smoke check: multi-size app icon builds from shipped resources."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sys
sys.path.insert(0, r"V:\___VAC\__K\__CODE\_PY\_AUDAPACK")

from audapack.ui_qt.app import _build_app_icon

ic = _build_app_icon()
print("icon is None:", ic is None)
if ic:
    print("sizes:", [(s.width(), s.height()) for s in ic.availableSizes()])
    print("has 16px:", ic.actualSize(__import__("PySide6.QtCore", fromlist=["QSize"]).QSize(16, 16)).width() > 0)