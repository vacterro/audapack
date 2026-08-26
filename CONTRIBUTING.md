# Contributing to AUDAPACK

Thank you for your interest in improving AUDAPACK! This guide outlines our development workflow, coding principles, and contribution guidelines.

---

## 🏛️ Core Principles

1. **Zero Runtime Bloat**: Core desktop packaging and CLI utilities rely exclusively on the Python standard library. Optional PySide6 Qt support remains strictly optional.
2. **Atomic & Resilient IO**: All file operations write to temporary `.part` files first, verify integrity (e.g. `zipfile.testzip()`), and atomically commit.
3. **Fail-Closed Security**: Network sockets bind exclusively to loopback (`127.0.0.1`), require local token authentication, and reject oversized payloads.
4. **Deterministic Golden Vintage UI**: Dark golden palette (`#332E22`, `#F0D060`, `#D4C89A`), 2px physical depth bevels, and zero font antialiasing (No AA) for high-speed readability.

---

## 🛠️ Development Setup

### 1. Prerequisites
- Python 3.10+ (Windows 10/11)
- Node.js 18+ (for testing the Tampermonkey browser widget)
- Git

### 2. Environment
```cmd
# Create virtual environment
python -m venv .venv
.venv\Scripts\activate

# Install development dependencies
pip install -e .[dev,qt]
```

### 3. Running the Test Suites
Before submitting any changes, verify all test suites pass:

```cmd
# Run Python backend and UI tests
python -m pytest

# Run Tampermonkey browser widget test suite
node --test tests/widget/*.test.js
```

---

## 📐 Code Style & Conventions

- Format code cleanly following PEP 8.
- Maintain full parity between English (`TRANSLATIONS_EN`) and Russian (`TRANSLATIONS_RU`) in `audapack/ui/i18n.py`.
- Run tests under `tests/test_i18n.py` to confirm parity.
- Include unit tests for every new feature, bug fix, or edge case.

---

## 🚀 Pull Request Workflow

1. Fork the repository and create a feature branch (`feat/your-feature` or `fix/your-fix`).
2. Implement your changes adhering to atomic IO and zero-bloat principles.
3. Add or update tests in `tests/`.
4. Ensure 100% of Python and Node.js tests pass.
5. Update `CHANGELOG.md` with a concise summary of changes.
6. Open a Pull Request against `main`.
