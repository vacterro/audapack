# AUDAPACK

<p align="center">
  <img src="resources/app_icon.png" width="128" height="128" alt="AUDAPACK logo">
</p>

<p align="center"><strong>Windows desktop cockpit for verified ZIP packaging, multi-wave AI audit handoff, and a local browser bridge.</strong></p>

<p align="center">
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/release-v0.1.2-D4B86A?style=for-the-badge" alt="Release v0.1.2"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10%2B-332E22?style=for-the-badge&logo=python&logoColor=D4B86A" alt="Python 3.10+"></a>
  <img src="https://img.shields.io/badge/platform-Windows-332E22?style=for-the-badge&logo=windows&logoColor=D4B86A" alt="Windows">
  <a href="tests/"><img src="https://img.shields.io/badge/Python%20tests-230%20PASS-4A7A20?style=for-the-badge&logo=pytest&logoColor=white" alt="230 Python tests passing"></a>
  <a href="tests/widget/"><img src="https://img.shields.io/badge/Widget%20tests-122%20PASS-4A7A20?style=for-the-badge&logo=javascript&logoColor=white" alt="122 widget tests passing"></a>
</p>

<p align="center"><a href="README.md"><strong>English</strong></a> · <a href="README.ru.md">Русский</a></p>

![AUDAPACK Project Room](resources/screenshot.png)

## What it does

AUDAPACK keeps projects, audit evidence, and distributable archives in one compact Windows workspace. The Qt interface is the production default; Tkinter remains available as an explicit fallback.

- **Verified ZIP packaging** — atomic `.part` staging, CRC verification, mandatory exclusions, archive manifests, retention, and human-readable archive sizes.
- **Project Room** — 24 canonical slots across `MAIN0`, `MAIN1`, `SIDE0`, and `SIDE1`, with additional priority groups supported when needed.
- **Quick3 and Super10 campaigns** — data-driven audit profiles with wave progress, run isolation, lineage checks, and canonical final handoffs.
- **Local Bridge** — loopback HTTP service on `127.0.0.1:17843`; API v3 is current and API v2 remains supported for compatibility.
- **Tampermonkey widget** — sends audit waves from ChatGPT, recovers interrupted runs, and keeps each campaign bound to its own run ID.
- **Windows integration** — Explorer context-menu packaging, silent VBScript launchers, clipboard handoff, and Scheduled Task support for the Bridge.
- **Golden Vintage UI** — dark Windows 95-inspired colors, compact beveled controls, and intentionally crisp text rendering.

## Install

AUDAPACK targets Windows and requires Python 3.10 or newer. Install the application with its Qt interface and development tools:

```powershell
python -m pip install -e ".[qt,dev]"
```

For runtime-only installation, use `.[qt]`. PySide6 is optional at package level, but required for the default Qt interface.

## Start

Double-click `AUDAPACK.vbs` for a silent GUI launch, or run the entry point directly:

```powershell
python AUDAPACK.pyw
```

The Qt interface is the default. Use `--ui tkinter` only when the legacy Tkinter interface is required:

```powershell
python AUDAPACK.pyw --ui tkinter
```

Install the browser side separately by adding `resources/AUDAPACK_WIDGET.user.js` to Tampermonkey, then open ChatGPT. The widget connects to the local Bridge when it is running.

## Project Room

Each occupied project row can show audit progress, audit age, archive freshness, ZIP size, copy counters, and pack state. ZIP sizes use binary units (`B`, `KB`, `MB`, `GB`, `TB`) and are refreshed after packaging. Full rows keep detailed ZIP metadata on a second line; enable **Settings → General → Compact project rows** to fit the essential status and size on one line.

Project actions include:

- `E` — include or exclude the project from packaging.
- `✓` — mark a project done/dimmed in the room.
- `A` — skip the project during archive-all operations.
- `PACK` — create or refresh one project archive.
- `PACK ALL` — package enabled projects in slot order.
- `COPY AUDIT` — copy the canonical audit handoff.
- `COPY ZIP` — copy the archive as a file to the Windows clipboard.
- `ⓘ` — open the complete project, audit, archive, and campaign details.

Archives can be stored in one output folder, beside each project, or in priority-group subfolders. Configure this under **Settings → Packing**.

## Browser audit flow

The widget supports two canonical profiles:

- **Quick3** — three-wave audit flow for the standard Core, Second, and Performance review.
- **Super10** — ten-wave deep audit with campaign synthesis and final implementation handoff.

The Bridge validates authenticated requests, project/run identity, profile manifests, wave order, and completion evidence before writing artifacts. Interrupted widget sessions can recover their durable state instead of silently starting a different run.

## Command-line reference

```text
usage: AUDAPACK.pyw [-h] [--pack PATH] [--pack-project ID] [--silent]
                    [--install-context-menu] [--remove-context-menu]
                    [--status] [--paste] [--ingest PATH_OR_TEXT] [--bridge]
                    [--takeover-legacy-bridge] [--install-autostart]
                    [--remove-autostart] [--repair-autostart]
                    [--ui {qt,tkinter}]

options:
  -h, --help              Show this help message and exit
  --pack PATH             Pack a directory or file into an archive
  --pack-project ID       Pack a registered project by ID
  --silent                Pack all enabled projects without opening the GUI
  --install-context-menu  Install the Explorer context-menu entry
  --remove-context-menu   Remove the Explorer context-menu entry
  --status                Print registry and audit status
  --paste                 Ingest audit wave(s) from the Windows clipboard
  --ingest PATH_OR_TEXT   Ingest audit wave(s) from a file or text string
  --bridge                Run the Bridge server in the foreground
  --takeover-legacy-bridge
                          Perform the transactional legacy Bridge takeover
  --install-autostart     Install the AUDAPACK Bridge Scheduled Task
  --remove-autostart      Remove the AUDAPACK Bridge Scheduled Task
  --repair-autostart      Repair the AUDAPACK Bridge Scheduled Task
  --ui {qt,tkinter}       Select the GUI: Qt (default) or Tkinter fallback
```

Examples:

```powershell
python AUDAPACK.pyw --pack "C:\Projects\Demo"
python AUDAPACK.pyw --pack-project AUDAPACK
python AUDAPACK.pyw --silent
python AUDAPACK.pyw --status
python AUDAPACK.pyw --bridge
```

## Test and lint

```powershell
python -m pytest -q
ruff check audapack tests
Get-ChildItem tests/widget -Filter *.test.js | ForEach-Object { node $_.FullName }
```

Current baseline: **230 Python tests** and **122 Node widget tests across 17 suites**.

## Repository map

```text
AUDAPACK/
├── audapack/
│   ├── bridge/             # Authenticated loopback Bridge and storage
│   ├── components/         # Widget, migration, and Windows integration
│   ├── services/           # Application, audit, project, and pack services
│   ├── ui_qt/              # Default PySide6 Project Room
│   ├── ui/                 # Tkinter fallback interface
│   ├── audits.py           # Audit indexing and snapshot handling
│   ├── campaign.py         # Quick3/Super10 campaign engine
│   ├── config.py           # Runtime configuration and migration
│   ├── ingest.py           # Validated, transactional audit ingest
│   ├── packing.py          # Atomic ZIP creation and archive discovery
│   └── projects.py         # Project registry and slot management
├── docs/wiki/              # Architecture, campaign, UI, and CLI guides
├── resources/              # Widget, icons, and Project Room screenshot
├── tests/                  # Python and Node regression suites
├── AUDAPACK.pyw            # Main entry point
├── AUDAPACK.vbs            # Silent GUI launcher
├── PACK_ALL_SILENT.vbs     # Silent batch-pack launcher
├── CHANGELOG.md            # Release history
├── README.md               # English documentation
├── README.ru.md            # Russian documentation
└── VERSION                 # Canonical version
```

## Documentation

- [Wiki home](docs/wiki/Home.md)
- [Architecture and Bridge](docs/wiki/Architecture-and-Bridge.md)
- [Audit Campaign Engine](docs/wiki/Audit-Campaign-Engine.md)
- [Auto3 audit pipeline](docs/wiki/Auto3-Audit-Pipeline.md)
- [CLI and silent packaging](docs/wiki/CLI-and-Silent-Packaging.md)
- [Golden Vintage UI](docs/wiki/UI-Golden-Vintage.md)
- [Widget regression suites](tests/widget/README.md)
- [Changelog](CHANGELOG.md)

## Safety guarantees

- Archives are written to a temporary `.part` file, verified, and committed atomically.
- Incomplete or invalid archives do not replace the previous good archive.
- The Bridge binds to loopback and requires an authentication token stored outside the project tree.
- Audit writes use transactional snapshots and report persistence failures instead of claiming success.
- Mandatory excludes prevent secrets, runtime state, caches, and nested archives from entering packages.
