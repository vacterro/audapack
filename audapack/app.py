"""Main application entry point and CLI router for AUDAPACK."""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path
from typing import Optional

from audapack import __app_name__, __version__
from audapack.audits import AuditIndexer
from audapack.config import app_dir, load_config, save_config
from audapack.context_menu import (
    install_context_menu,
    is_context_menu_installed,
    remove_context_menu,
)
from audapack.packing import pack_single
from audapack.projects import ProjectRegistry
from audapack.saipen import get_saipen_info


def run_silent_pack_all() -> int:
    """Packs all enabled projects in silent mode with rotating/appended log."""
    config = load_config()
    registry = ProjectRegistry(config)
    log_file = app_dir() / "pack_all_audit_silent.log"

    def log(msg: str):
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"{msg}\n")

    log(f"--- {__app_name__} v{__version__} SILENT RUN START ---")
    enabled_projects = [p for p in registry.projects if p.enabled]
    if not enabled_projects:
        log("No enabled projects to pack.")
        return 0

    excludes = set(config.packing.excludes)
    output_dir = Path(config.packing.output_dir or str(app_dir()))
    all_success = True

    for p in enabled_projects:
        if not p.source_path or not p.source_path.strip() or not Path(p.source_path).exists():
            log(f"SKIP {p.display_name}: Source path missing or unusable ({p.source_path})")
            # An enabled project that cannot be packed is a failed requested pack,
            # not a benign skip: automation must see a nonzero aggregate result.
            all_success = False
            continue

        extra_meta = {}
        if config.packing.manifest_enabled:
            saipen_info = get_saipen_info(p.source_path)
            extra_meta["saipen_detected"] = saipen_info.detected
            if saipen_info.detected:
                extra_meta["git"] = {
                    "branch": saipen_info.git_branch,
                    "head": saipen_info.git_head,
                    "dirty": saipen_info.git_dirty,
                    "changed_files": saipen_info.git_changed_files,
                }

        res = pack_single(
            source_path=p.source_path,
            output_dir=output_dir,
            archive_stem=p.archive_name or p.display_name,
            excludes=excludes,
            delete_old=config.packing.delete_old,
            log_callback=log,
            manifest_meta={"project_name": p.display_name, "extra_meta": extra_meta} if config.packing.manifest_enabled else None,
        )
        if not res.success:
            all_success = False

    log(f"--- {__app_name__} SILENT RUN FINISHED (Success: {all_success}) ---\n")
    return 0 if all_success else 1


def run_pack_path(path_str: str) -> int:
    """Packs a single target path (folder or file) from CLI or Explorer context menu."""
    config = load_config()
    target = Path(path_str).resolve()
    if not target.exists():
        print(f"Error: Target path does not exist: {target}", file=sys.stderr)
        return 1

    registry = ProjectRegistry(config)
    matching_project = registry.get_project_by_path(target)
    stem = matching_project.archive_name if matching_project else target.name
    output_dir = Path(config.packing.output_dir or str(app_dir()))
    excludes = set(config.packing.excludes)

    extra_meta = {}
    if config.packing.manifest_enabled:
        saipen_info = get_saipen_info(target)
        extra_meta["saipen_detected"] = saipen_info.detected
        if saipen_info.detected:
            extra_meta["git"] = {
                "branch": saipen_info.git_branch,
                "head": saipen_info.git_head,
                "dirty": saipen_info.git_dirty,
                "changed_files": saipen_info.git_changed_files,
            }

    res = pack_single(
        source_path=target,
        output_dir=output_dir,
        archive_stem=stem,
        excludes=excludes,
        delete_old=config.packing.delete_old,
        manifest_meta={"project_name": stem, "extra_meta": extra_meta} if config.packing.manifest_enabled else None,
    )
    if res.success:
        print(f"Successfully packed {target} -> {res.output_path}")
        return 0
    else:
        print(f"Failed to pack {target}: {res.error_message}", file=sys.stderr)
        return 1


def run_pack_project(project_id: str) -> int:
    """Packs a registered project by its ID using the resolved project identity."""
    config = load_config()
    registry = ProjectRegistry(config)
    proj = registry.get_project_by_id(project_id)
    if not proj:
        print(f"Error: Project '{project_id}' not found in registry.", file=sys.stderr)
        return 1

    # CORE-003: stay ID-stable. Pack from the already resolved project instead of
    # re-resolving its path (which could pick another project's archive identity),
    # and reject blank sources before any Path.resolve() so an empty source can
    # never fall back to the current working directory.
    if not proj.source_path or not proj.source_path.strip():
        print(f"Error: Project '{project_id}' has no source path configured.", file=sys.stderr)
        return 1

    source = Path(proj.source_path)
    if not source.exists():
        print(f"Error: Source path does not exist: {source}", file=sys.stderr)
        return 1

    output_dir = Path(config.packing.output_dir or str(app_dir()))
    excludes = set(config.packing.excludes)
    stem = proj.archive_name or proj.display_name

    extra_meta = {}
    if config.packing.manifest_enabled:
        saipen_info = get_saipen_info(source)
        extra_meta["saipen_detected"] = saipen_info.detected
        if saipen_info.detected:
            extra_meta["git"] = {
                "branch": saipen_info.git_branch,
                "head": saipen_info.git_head,
                "dirty": saipen_info.git_dirty,
                "changed_files": saipen_info.git_changed_files,
            }

    res = pack_single(
        source_path=source,
        output_dir=output_dir,
        archive_stem=stem,
        excludes=excludes,
        delete_old=config.packing.delete_old,
        manifest_meta={"project_name": stem, "extra_meta": extra_meta} if config.packing.manifest_enabled else None,
    )
    if res.success:
        print(f"Successfully packed {source} -> {res.output_path}")
        return 0
    else:
        print(f"Failed to pack {source}: {res.error_message}", file=sys.stderr)
        return 1


def print_status() -> int:
    config = load_config()
    registry = ProjectRegistry(config)
    indexer = AuditIndexer(config)
    snapshots = indexer.scan_all_projects()

    print(f"{__app_name__} v{__version__} Status")
    print(f"Audit Root: {config.audits.root}")
    print(f"Output Dir: {config.packing.output_dir}")
    print(f"Context Menu Installed: {is_context_menu_installed()}")
    print("-" * 50)
    for p in registry.projects:
        snap = snapshots.get(p.id)
        ready_str = f"{snap.completed_waves}/3" if snap else "0/3"
        all_str = "ALL" if (snap and snap.all3_ready) else ""
        temp_str = snap.temperature.value if snap else "NONE"
        print(f"[{p.priority_group} #{p.slot}] {p.display_name:<25} {ready_str:<4} {all_str:<4} {temp_str:<6} {p.source_path}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=f"{__app_name__} — Project archive packager & audit room.")
    parser.add_argument("--pack", metavar="PATH", help="Pack specified directory or file into archive")
    parser.add_argument("--pack-project", metavar="ID", help="Pack project by ID")
    parser.add_argument("--silent", action="store_true", help="Pack all enabled projects silently without UI")
    parser.add_argument("--install-context-menu", action="store_true", help="Install Explorer context menu entry")
    parser.add_argument("--remove-context-menu", action="store_true", help="Remove Explorer context menu entry")
    parser.add_argument("--status", action="store_true", help="Print registry and audit status to stdout")
    parser.add_argument("--paste", action="store_true", help="Ingest audit wave(s) from Windows clipboard")
    parser.add_argument("--ingest", metavar="PATH_OR_TEXT", help="Ingest audit wave(s) from file path or text string")
    parser.add_argument("--bridge", action="store_true", help="Run AUDAPACK bridge server in foreground")
    parser.add_argument("--takeover-legacy-bridge", action="store_true", help="Execute transactional takeover from legacy ACBBridge")
    parser.add_argument("--install-autostart", action="store_true", help="Install Windows Scheduled Task 'AUDAPACK Bridge'")
    parser.add_argument("--remove-autostart", action="store_true", help="Remove Windows Scheduled Task 'AUDAPACK Bridge'")
    parser.add_argument("--repair-autostart", action="store_true", help="Repair Windows Scheduled Task 'AUDAPACK Bridge'")
    parser.add_argument("--ui", choices=["qt", "tkinter"], default="qt", help="GUI framework: qt (PySide6, default) or tkinter (legacy fallback)")

    args = parser.parse_args(argv)

    if args.paste:
        import tkinter as tk
        from audapack.ingest import ingest_audit_text
        try:
            r = tk.Tk()
            r.withdraw()
            txt = r.clipboard_get()
            r.destroy()
        except Exception as e:
            print(f"Error reading clipboard: {e}", file=sys.stderr)
            return 1
        res = ingest_audit_text(txt)
        print(res.message if res.ok else f"Error: {res.error}")
        return 0 if res.ok else 1

    if args.ingest:
        from audapack.ingest import ingest_audit_text
        p = Path(args.ingest)
        if p.exists() and p.is_file():
            txt = p.read_text(encoding="utf-8", errors="replace")
        else:
            txt = args.ingest
        res = ingest_audit_text(txt)
        print(res.message if res.ok else f"Error: {res.error}")
        return 0 if res.ok else 1

    if args.takeover_legacy_bridge:
        from audapack.components.migration import perform_bridge_takeover
        ok, rep = perform_bridge_takeover()
        print("Takeover succeeded." if ok else f"Takeover failed: {rep.get('errors')}")
        return 0 if ok else 1

    if args.install_autostart:
        from audapack.components.autostart import install_autostart
        ok, msg = install_autostart()
        print(msg)
        return 0 if ok else 1

    if args.remove_autostart:
        from audapack.components.autostart import remove_autostart
        ok, msg = remove_autostart()
        print(msg)
        return 0 if ok else 1

    if args.repair_autostart:
        from audapack.components.autostart import repair_autostart
        ok, msg = repair_autostart()
        print(msg)
        return 0 if ok else 1

    if args.install_context_menu:
        ok = install_context_menu()
        print("Context menu installed successfully." if ok else "Failed to install context menu.")
        return 0 if ok else 1

    if args.remove_context_menu:
        ok = remove_context_menu()
        print("Context menu removed successfully." if ok else "Failed to remove context menu.")
        return 0 if ok else 1

    if args.status:
        return print_status()

    if args.pack:
        return run_pack_path(args.pack)

    if args.pack_project:
        return run_pack_project(args.pack_project)

    if args.silent:
        return run_silent_pack_all()

    if args.bridge:
        from audapack.bridge.server import run_bridge_server
        config = load_config()
        return run_bridge_server(config)

    # Launch GUI (enforce single instance)
    from audapack.single_instance import SingleInstance
    single = SingleInstance("AUDAPACK_GUI")
    if single.is_already_running():
        single.activate_existing_window("AUDAPACK")
        return 0

    # Qt is the production default (Wave N cutover). Tkinter remains only as explicit fallback.
    if args.ui == "tkinter":
        from audapack.ui.main_window import run_gui
        return run_gui()

    try:
        from audapack.ui_qt.app import run_qt_gui

        return run_qt_gui()
    except ImportError as exc:
        print(f"Qt (PySide6) not available ({exc}); falling back to Tkinter. Install with: pip install PySide6", file=sys.stderr)
        from audapack.ui.main_window import run_gui

        return run_gui()


if __name__ == "__main__":
    sys.exit(main())
