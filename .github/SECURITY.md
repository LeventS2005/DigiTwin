# Security policy

## Threat model

meshforge is a **local, single-user, offline** tool: it runs on your machine, the
web/desktop Workbench binds `127.0.0.1` only, and nothing is uploaded anywhere.
The user and their own CLI arguments are therefore **trusted** — the real trust
boundary is the **files you open** (`.dwg`/`.dxf`, `.stl`/`.ply`/`.vtp`, `.cso`,
DICOM/NIfTI/`.npy` volumes, `.mfwork` work files, recipe YAML) and the **output
of external solvers** (CalculiX `.frd`/`.dat`, MYSTRAN `.F06`, OpenFOAM cases),
all of which may be crafted or malformed. Against that boundary the project's
hard rule is **fail loudly, never fail open** — a bad input must raise, not
quietly produce a plausible-looking mesh or a corrupted solver deck.

Out of scope: multi-tenant or networked deployment (the app refuses any non-
loopback host), privilege boundaries, and the `MESHFORGE_CCX` /
`MESHFORGE_MYSTRAN` / `MESHFORGE_OPENFOAM` executable-path overrides — those are
documented, deliberately-trusted knobs, though the *output* of whatever they
point at is still treated as untrusted and sanitised before it reaches your
terminal.

## Reporting a vulnerability

Please report privately rather than opening a public issue: use
[GitHub's private vulnerability reporting](https://github.com/LeventS2005/meshforge/security/advisories/new)
on this repository. Include the crafted input (a *synthetic* one — never a
clinical file), the command or Workbench step that triggers it, and what went
wrong.

If that link is unavailable to you, open a public issue titled
`security contact request` containing **no details of the issue** — just ask for
a private channel — and you will be sent one.

This is a research project maintained by one person; expect a first response
within about two weeks, and no formal SLA beyond that.

Please do **not** send clinical or patient-derived data in a report under any
circumstances. Every finding to date has been reproduced with a tiny synthetic
fixture, and yours can be too.

## Audits and known findings

Two written audits live in [`docs/security/`](../docs/security/), each with a
status table giving the `file:line` that closes every finding:

| Report | Scope | Status |
|---|---|---|
| [`security-analysis-findings.md`](../docs/security/security-analysis-findings.md) | Whole codebase after the D6 baseline: deck/dictionary injection, terminal-escape injection, subprocess timeouts, allocation ceilings, output-path guards | F1–F4 fixed; F5 informational, accepted |
| [`security-cso-biomedical-findings.md`](../docs/security/security-cso-biomedical-findings.md) | The untrusted-binary ingest path: MeVisLab `.cso`, DICOM/NIfTI, `.vtp` field data, `.mfwork` replay — plus a 36k-mutation fuzz campaign | All findings fixed (two rounds) |

Both are regression-tested and ungated in CI:
[`tests/test_security_fixes.py`](../tests/test_security_fixes.py) and
[`tests/test_cso_security.py`](../tests/test_cso_security.py).

## Supported versions

Only the latest release on `main` receives fixes.
