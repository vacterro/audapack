# Canonical widget regression suites

These Node.js suites protect the single browser component:

```text
resources/AUDAPACK_WIDGET.user.js
```

They cover lease ownership, migration, audit classification, run lineage,
recovery, Quick3/Super10 campaigns, fresh-archive startup, terminal states,
gate recovery, manual-save persistence truth, manual archive-drop START
end-to-end, and performance-sensitive observer/drag paths.

Run all suites (the canonical full-suite gate):

```powershell
node --test tests/widget/*.test.js
```

Run one suite while debugging:

```powershell
node tests/widget/w3-002-fresh-archive-autostart.test.js
```

A failing suite is a regression; it must not be accepted as baseline behavior.
The canonical gate must print its tests/pass/fail summary, report zero
cancelled, and terminate the process: a run that prints results and then hangs
is still a gate failure. Per T-135, this file deliberately carries no test
total: a number nobody re-measures rots.
