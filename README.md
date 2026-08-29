# AUDAPACK

<p align="center">
  <img src="resources/app_icon.png" width="128" height="128" alt="AUDAPACK Logo">
</p>

<p align="center">
  <b>High-velocity Windows project packaging, audit cockpit & browser automation bridge</b>
</p>

<p align="center">
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/release-v0.1.2-D4B86A?style=for-the-badge&logo=github" alt="Release"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10+-332E22?style=for-the-badge&logo=python&logoColor=D4B86A" alt="Python 3.10+"></a>
  <img src="https://img.shields.io/badge/Platform-Windows-332E22?style=for-the-badge&logo=windows&logoColor=D4B86A" alt="Windows">
  <a href="tests/"><img src="https://img.shields.io/badge/Tests-162%20PASS-4A7A20?style=for-the-badge&logo=pytest&logoColor=white" alt="Pytest"></a>
  <a href="resources/AUDAPACK_WIDGET.user.js"><img src="https://img.shields.io/badge/Widget-86%20PASS-4A7A20?style=for-the-badge&logo=javascript&logoColor=white" alt="Widget"></a>
  <a href="docs/wiki/UI-Golden-Vintage.md"><img src="https://img.shields.io/badge/Theme-Golden%20Vintage-75663D?style=for-the-badge" alt="Golden Vintage"></a>
</p>

<p align="center">
  <b><a href="README.md">English</a></b> • <b><a href="README.ru.md">Русский</a></b>
</p>

---

<p align="center">
  <img width="640" height="540" alt="2026-08-30_025740" src="https://github.com/user-attachments/assets/dbaf0e39-c925-4baa-8edb-7e36d706fd02" />
</p>

---

## ⚡ Highlights

- **📦 Clean Project Packaging**: Timestamped, CRC-verified `.zip` creation with `.part` staging, exclude filtering, and optional metadata manifests.
- **🎛️ 24-Slot Priority Cockpit**: Structured grid across four canonical groups (`MAIN0`, `MAIN1`, `SIDE0`, `SIDE1`) with 6 slots each.
- **⏱️ Real-Time Freshness Tracking**: Color-coded audit temperature indicators (`HOT`, `WARM`, `COOL`, `COLD`, `STALE`) showing exact elapsed time.
- **📋 1-Click Audit Handoff**: Copies canonical `__00_AUDIT_ALL_3.md` instantly, tracks hash state, and switches between `✓ AUDIT` and `AUDIT`.
- **🌐 Browser Auto3 Automation**: Bundled Tampermonkey userscript (`AUDAPACK_WIDGET.user.js`) automating 3-wave audits in ChatGPT with strict `runId` boundary isolation.
- **🔌 Loopback Bridge Daemon**: High-throughput HTTP server on `127.0.0.1:17843` (API v2) with token authorization and atomic wave aggregation.
- **🪟 Windows Integration**: Explorer right-click context menu integration (*"Упаковать через AUDAPACK"*) and silent VBScript background launchers.
- **🎨 Golden Vintage Aesthetic**: Authentic Windows 95 Dark Golden theme with 2px raised/sunken bevels and zero antialiasing for maximum readability.

---

## 🧭 Cockpit Grid Layout

The 24-slot interface organizes projects into a dense, high-contrast operational grid:

| Column | Header | Description | Interaction |
|:---|:---|:---|:---|
| **0** | `✓ ⊘` | Enable / Visual Dimming Checkboxes | Toggle packing inclusion or visual dimming |
| **1** | `SLOT` | Priority Slot Number (`#1`–`#6`) | Drag handle and priority position |
| **2** | `Project & Path` | Name, Git Dirty badge, SAIPEN status, Source Path | Right-click name to clear copied state |
| **3** | `WAVE` | Audit Wave Progress (`✓ 3/3`, `2/3`, `1/3`, `0/3`) | Visual status of current audit stage |
| **4** | `FRESHNESS` | Temperature Marker (`● 14m`, `● 3h`, `● 8h`, `—`) | Color-coded age of latest audit |
| **5** | `AUDIT` | Copy Audit Handoff Button | Copies `__00_AUDIT_ALL_3.md` to clipboard |
| **6** | `PACK` | Single-Project Pack Button | Creates immediate timestamped `.zip` archive |
| **7** | `ARCHIVE` | Copy Archive File Button (`ARCHIVE (14m)`) | Copies `.zip` file directly to clipboard |
| **8** | `···` | Project Context Menu | Move, Edit, Mute, Open Folder, Delete |

---

## 🚀 Quick Start

### 1. Launch GUI
Double-click `AUDAPACK.vbs` (silent background start, no black console window) or run:
```cmd
pythonw AUDAPACK.pyw
```

### 2. Silent All-Project Packaging
Double-click `PACK_ALL_SILENT.vbs` or run:
```cmd
pythonw AUDAPACK.pyw --silent
```

### 3. Explorer Context Menu
Install the context menu from **Settings** inside the GUI, or via command line:
```cmd
python AUDAPACK.pyw --install-context-menu
```
*Right-click any folder or file in Windows Explorer and select **Упаковать через AUDAPACK**.*

### 4. Install Browser Widget
Open Tampermonkey in your browser and install `resources/AUDAPACK_WIDGET.user.js`. When opening ChatGPT, the AUDAPACK toolbar will attach to the prompt input.

---

## 🌡️ Freshness & Temperature Matrix

Audit temperature is dynamically computed from metadata timestamps (`GENERATED_AT` / `DATE_TIME`) in the audit files:

| Marker | Temperature | Age Threshold | Color / Visual Tone |
|:---:|:---|:---|:---|
| `●` | **HOT** | `0` – `4 hours` | Coral Red (`#D49090` on `#451B1B`) |
| `●` | **WARM** | `>4` – `24 hours` | Golden Amber (`#D4B875` on `#3E3014`) |
| `●` | **COOL** | `>1` – `3 days` | Steel Blue (`#8BB4D4` on `#182E40`) |
| `❄️` | **COLD** | `>3` – `7 days` | Slate Ice (`#A0A8B0` on `#20242B`) |
| `○` | **STALE** | `>7 days` | Muted Dark (`#7D7565` on `#221E18`) |
| `—` | **NONE** | *No audit found* | Muted Dash (`#6E674E`) |

---

## 💻 CLI Reference

```text
usage: AUDAPACK.pyw [-h] [--pack PATH] [--pack-project ID] [--silent]
                    [--install-context-menu] [--remove-context-menu]
                    [--status] [--bridge]

options:
  -h, --help              Show this help message and exit
  --pack PATH             Pack specified directory or file into archive
  --pack-project ID       Pack project by ID from registry
  --silent                Pack all enabled projects silently without UI
  --install-context-menu  Install Windows Explorer context menu entry
  --remove-context-menu   Remove Windows Explorer context menu entry
  --status                Print registry and audit freshness status to stdout
  --bridge                Run AUDAPACK bridge server in foreground
```

---

## 📁 Repository Structure

```text
_AUDAPACK/
├── audapack/               # Core Python application package
│   ├── bridge/             # Local HTTP daemon (API v2) & wave storage
│   ├── components/         # Scheduled tasks, autostart & migration
│   ├── services/           # Framework-neutral application services
│   ├── ui/                 # Tkinter Golden Default desktop UI
│   ├── ui_qt/              # PySide6 Qt desktop implementation
│   ├── config.py           # Configuration & JSON serializer
│   ├── packing.py          # Atomic ZIP packager with .part staging
│   └── projects.py         # 24-slot registry & priority groups
├── docs/                   # Documentation & developer wiki
│   └── wiki/               # 5-part comprehensive documentation
├── resources/              # Brand assets, icons & Tampermonkey widget
│   ├── AUDAPACK_WIDGET.user.js # Browser automation userscript
│   ├── app_icon.ico        # Multi-size Windows application icon
│   ├── app_icon.png        # Golden Vintage application icon
│   └── screenshot.png      # High-resolution cockpit screenshot
├── scripts/                # Benchmarking & performance tools
├── tests/                  # Pytest & Node widget test suites
│   ├── services/           # Neutral service unit tests
│   ├── ui/                 # Model & UI component tests
│   └── widget/             # 86 Node.js browser widget unit tests
├── AUDAPACK.pyw            # Main GUI entry point
├── AUDAPACK.vbs            # Silent GUI launcher
├── PACK_ALL_SILENT.vbs     # Silent batch pack launcher
├── CHANGELOG.md            # Monotonic release changelog
├── README.md               # English documentation
├── README.ru.md            # Russian documentation
└── VERSION                 # Canonical semver release version
```

---

## 📚 Documentation Wiki

Detailed guides are available in [`docs/wiki/`](docs/wiki/):
- 🏠 **[Wiki Home](docs/wiki/Home.md)** — Getting started and overview.
- 🔌 **[Architecture & Bridge Daemon](docs/wiki/Architecture-and-Bridge.md)** — HTTP endpoints and security isolation.
- 🤖 **[Auto3 Audit Pipeline](docs/wiki/Auto3-Audit-Pipeline.md)** — 3-wave audit lifecycle and userscript mechanics.
- 🎨 **[Golden Vintage UI Design](docs/wiki/UI-Golden-Vintage.md)** — Win95 palette tokens and pixel-crisp rules.
- 📦 **[CLI & Silent Packaging](docs/wiki/CLI-and-Silent-Packaging.md)** — Advanced automation and scripting.

---

## 🔒 Invariants & Safety

- **Atomic Staging**: Archives are written to `.part` temporary files first, validated with `zipfile.testzip()`, and only then committed to destination.
- **Fail-Closed Security**: The HTTP bridge strictly binds to loopback (`127.0.0.1`), requires a 256-bit authentication token stored outside project source in `%LOCALAPPDATA%`, and enforces request size boundaries.
- **Strict RunId Isolation**: Audit handoffs enforce run-boundary separation to prevent cross-run wave badge bleed.
- **Zero Heavy Frameworks**: Core functionality runs on Python standard library without cloud dependencies or telemetry.

---

<img width="640" height="540" alt="2026-08-30_025746" src="https://github.com/user-attachments/assets/e6b25b21-4816-483d-9b74-f61257af0392" />
<img width="640" height="540" alt="2026-08-30_025749" src="https://github.com/user-attachments/assets/f02df212-ca4c-42c1-810b-4f2ea3c5bac3" />
<img width="640" height="540" alt="2026-08-30_025756" src="https://github.com/user-attachments/assets/72644503-6ae4-42d2-9ff9-25c6a44cd6f7" />


<p align="center">
  <b>AUDAPACK</b> — Built for speed, clarity, and reliability.
</p>

