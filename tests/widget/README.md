# Canonical widget regression suites

These Node.js suites protect the single browser component:

```text
resources/AUDAPACK_WIDGET.user.js
```

They cover lease ownership, migration, audit classification, run lineage,
recovery, Quick3/Super10 campaigns, fresh-archive startup, terminal states,
gate recovery, and performance-sensitive observer/drag paths.

Run one suite:

```powershell
node tests/widget/w3-002-fresh-archive-autostart.test.js
```

Run all 17 suites:

```powershell
Get-ChildItem tests/widget -Filter *.test.js |
  ForEach-Object { node $_.FullName }
```

Current baseline: **122 passing tests across 17 suites**. A failing suite is a
regression; it must not be accepted as baseline behavior.
