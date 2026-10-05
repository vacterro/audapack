# AUDAPACK

**v0.3.1**

<p align="center">
  <img src="resources/app_icon.png" width="128" height="128" alt="AUDAPACK Logo">
</p>

<p align="center">
  <b>High-velocity Windows project packaging, audit cockpit & browser automation bridge</b>
</p>

<p align="center">
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/release-v0.3.1-D4B86A?style=for-the-badge&logo=github" alt="Release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-4A7A20?style=for-the-badge" alt="MIT License"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10+-332E22?style=for-the-badge&logo=python&logoColor=D4B86A" alt="Python 3.10+"></a>
  <img src="https://img.shields.io/badge/Platform-Windows-332E22?style=for-the-badge&logo=windows&logoColor=D4B86A" alt="Windows">
  <a href="tests/"><img src="https://img.shields.io/badge/Tests-passing-4A7A20?style=for-the-badge&logo=pytest&logoColor=white" alt="Pytest suite passing"></a>
  <a href="resources/AUDAPACK_WIDGET.user.js"><img src="https://img.shields.io/badge/Widget-passing-4A7A20?style=for-the-badge&logo=javascript&logoColor=white" alt="Widget"></a>
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
- **🔌 Loopback Bridge Daemon**: High-throughput HTTP server on `127.0.0.1:17843` (API v3/v2) with token authorization, INAUDIT capture REST API, and atomic wave aggregation.
- **🪟 Windows Integration**: Explorer right-click context menu integration (*"Упаковать через AUDAPACK"*) and silent VBScript background launchers.
- **🎨 Golden Vintage Aesthetic**: Authentic Windows 95 Dark Golden theme with 2px raised/sunken bevels and zero antialiasing for maximum readability.
- **📥 Durable INAUDIT Inbox**: Filesystem-backed capture store for ChatGPT responses, blocks, or clipboard text; deterministic project classification, canonical SAIPEN enqueue for managed projects, and archive/restore/delete lifecycle.
- **🧠 Dedicated Chromium Worker**: Launches a Chromium-family browser (Chrome, Edge, Vivaldi, Opera) in an isolated profile with all throttling disabled; the top-level-only Widget guard rejects embedded ChatGPT sentinel frames.

### 🔗 SAI Accounts is optional

AUDAPACK works on its own. If the **SAI Accounts** control plane happens to be installed on this machine, AUDAPACK picks up its shared accounts as extra registry rows — automatically, no setting, no import step. If the plane is absent, stopped, broken or uninstalled, AUDAPACK discovers accounts exactly as it always did. **Nothing in this README requires anything else to be installed.**

Four rules govern the optional federation:

- **Your accounts stay yours.** A shared account appears *alongside* your own; it never replaces one. Shared rows carry `discovery_source = "sai_accounts_shared"` and a `<provider>:shared:<digest>` id, a namespace that can never collide with a locally discovered row.
- **Merge only on proven identity.** A shared row and a local row collapse into one only when a stable identity locator proves they are the same account — the config-directory path for Claude/Codex, the Windows account name for Antigravity. A matching display name is never evidence; where identity cannot be proven, both rows are shown separately.
- **Unavailable with a reason, never silently re-read.** A shared account is read *through* the plane, because the plane owns that identity and its execution context. If the plane cannot answer, the row reports `shared_source_offline`, `offline:…`, `auth_required:…` or `unavailable:…` — it never falls back to reading the local CLI, which would report *this* machine's account under someone else's name.
- **Local settings stay local.** Which launchers an account is bound to, and whether it is enabled here, are AUDAPACK's own settings. Hiding or disabling an account in the shared registry is a *global* action, and is a separate concept from a local one.

Discovery and probing stay credential-free: the plane is queried for account metadata only, and no token, session cookie or credential blob is ever read, copied, exported or written.

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

The userscript's `@version` is the only thing Tampermonkey compares when deciding whether an installed copy is current, so new widget bytes always ship under a new version: the bundled script is paired with a committed release ledger (`resources/AUDAPACK_WIDGET.release.json`) recording the version and SHA-256 that shipped together. Record a release with `python scripts/update_widget_release.py`, and confirm the Bridge is offering it with `python scripts/widget_update_probe.py --installed-version <dashboard version>`. The manual browser steps live in [`docs/AUDAPACK_WIDGET_ACCEPTANCE.md`](docs/AUDAPACK_WIDGET_ACCEPTANCE.md).

### 5. Launch a Dedicated Chromium Audit Worker
Use **Settings → Components → Launch AUDAPACK Chromium**. AUDAPACK picks an installed Chromium-family browser (Google Chrome, Cent, Edge, Vivaldi, or Opera before Brave), launches it with an isolated profile under `%LOCALAPPDATA%\AUDAPACK\browser_worker`, and disables Chromium's background timer, occlusion, and renderer throttling. Install Tampermonkey and the widget once inside that dedicated profile. The worker keeps running minimized, covered by other apps, or with the displays off; Windows sleep still suspends every process.

Only a clean root ChatGPT tab in a Chromium-family browser can claim a new audit — existing conversations, drafts, attachments, in-flight generation, and non-root URLs fail closed, and embedded ChatGPT sentinel frames are rejected outright.

### 6. INAUDIT Capture Workflow
On a stable ChatGPT answer, press `IA` beside the response or a code block. A verified Bridge write shows `IA ✓`; if the Bridge is unavailable, the bounded IndexedDB spool shows `IA QUEUED` and retries the same capture identity later. In AUDAPACK, open **INAUDIT → Inbox** to inspect provenance and classification evidence, choose a registered project, then use **Assign** or **Assign + CC**. The toolbar's `IA+` action captures the current Windows clipboard through the same durable store.

Audit replies need no click: when a reply the widget watched stream finishes and the message it answers carried a project ZIP, the reply is captured to the Inbox automatically and pinned to the project that owns that archive name. Short replies, old conversations being reopened and worker-driven runs are skipped. Settings → Bridge → "Auto-capture audit replies to INAUDIT" turns it off.

For projects containing `.saipen/`, AUDAPACK calls the CLI bound by that project's `STATE.md`: `saipen audit enqueue --producer audapack --operation-id <capture UUID> --item-id <capture UUID> --file <capture body>`. SAIPEN allocates the layer number and owns publication. Failed delivery retains the capture; retry uses the same UUID and cannot recreate a consumed layer. A missing or broken SAIPEN binding reports an error without falling back to local allocation. Finished audit mirrors use the same producer API with a stable content-derived operation UUID. Projects without SAIPEN retain local layer delivery.

Run the copied `saipen cc` in the selected project's agent session. It processes the Audit Inbox in protocol order; selecting a row does not override active Work or select that audit for immediate execution. Legacy INAUDIT GG buttons also copy bare `saipen cc`. After Source closure, Work DONE and matching bytes, SAIPEN consumes the layer automatically on the next continuation. Layers refresh from disk; an unsaved editor draft is retained until Save or Reload, and Save refuses an already changed or consumed layer. Manual Delete remains an explicit operator action. Closed audit evidence stays in SAIPEN's source archive; Layers is the live queue.

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
│   └── wiki/               # Developer wiki, one file per subsystem
├── resources/              # Brand assets, icons & Tampermonkey widget
│   ├── AUDAPACK_WIDGET.user.js # Browser automation userscript
│   ├── app_icon.ico        # Multi-size Windows application icon
│   ├── app_icon.png        # Golden Vintage application icon
│   └── screenshot.png      # High-resolution cockpit screenshot
├── scripts/                # Benchmarking & performance tools
├── tests/                  # Pytest & Node widget test suites
│   ├── services/           # Neutral service unit tests
│   ├── ui/                 # Model & UI component tests
│   └── widget/             # Node.js browser widget unit tests
├── AUDAPACK.pyw            # Main GUI entry point
├── AUDAPACK.vbs            # Silent GUI launcher
├── PACK_ALL_SILENT.vbs     # Silent batch pack launcher
├── CHANGELOG.md            # Monotonic release changelog
├── README.md               # English documentation
├── README.ru.md            # Russian documentation
└── VERSION                 # Release version, kept equal to pyproject and __init__
```

---

## 📚 Documentation Wiki

Detailed guides are available in [`docs/wiki/`](docs/wiki/):
- 🏠 **[Wiki Home](docs/wiki/Home.md)** — Getting started and overview.
- 🔌 **[Architecture & Bridge Daemon](docs/wiki/Architecture-and-Bridge.md)** — HTTP endpoints and security isolation.
- 🤖 **[Auto3 Audit Pipeline](docs/wiki/Auto3-Audit-Pipeline.md)** — 3-wave audit lifecycle and userscript mechanics.
- 🎨 **[Golden Vintage UI Design](docs/wiki/UI-Golden-Vintage.md)** — Win95 palette tokens and pixel-crisp rules.
- 📦 **[CLI & Silent Packaging](docs/wiki/CLI-and-Silent-Packaging.md)** — Advanced automation and scripting.
- 🎯 **[Audit Campaign Engine](docs/wiki/Audit-Campaign-Engine.md)** — Campaign profiles, wave definitions and the run manifest.

---

## 🔒 Invariants & Safety

- **Atomic Staging**: Archives are written to `.part` temporary files first, validated with `zipfile.testzip()`, and only then committed to destination.
- **Fail-Closed Security**: The HTTP bridge strictly binds to loopback (`127.0.0.1`), requires a 256-bit authentication token stored outside project source in `%LOCALAPPDATA%`, and enforces request size boundaries.
- **Strict RunId Isolation**: Audit handoffs enforce run-boundary separation to prevent cross-run wave badge bleed.
- **Zero Heavy Frameworks**: Core functionality runs on Python standard library without cloud dependencies or telemetry.

---

## 🤝 Contributing & License

- Contributing guidelines: [`CONTRIBUTING.md`](CONTRIBUTING.md).
- Security reports and the supported disclosure path: [`SECURITY.md`](SECURITY.md).
- Released under the [MIT License](LICENSE).

---

<img width="640" height="540" alt="2026-08-30_025746" src="https://github.com/user-attachments/assets/e6b25b21-4816-483d-9b74-f61257af0392" />
<img width="640" height="540" alt="2026-08-30_025749" src="https://github.com/user-attachments/assets/f02df212-ca4c-42c1-810b-4f2ea3c5bac3" />
<img width="640" height="540" alt="2026-08-30_025756" src="https://github.com/user-attachments/assets/72644503-6ae4-42d2-9ff9-25c6a44cd6f7" />


<p align="center">
  <b>AUDAPACK</b> — Built for speed, clarity, and reliability.
</p>

