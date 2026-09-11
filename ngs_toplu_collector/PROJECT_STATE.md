# PROJECT STATE — NGS Mutasyon Havuzu

**Public source version:** 0.2.4  
**Status:** collector + archive + mutation viewer + POOL spread analysis are operational.

This file is intentionally maintained as a restart point. If the local project directory or the development chat is lost, start here.

## Current architecture

- Python desktop UI: Tkinter/ttk.
- Browser automation: Playwright persistent Chromium profile.
- Exactly one browser worker thread owns all Playwright objects.
- Main Runs page stays open; child pages are closed after each task.
- State/index database: `DATA/collector_state.sqlite3` (local only; never commit).
- Raw downloads: `OUTPUT/` (local only; never commit).
- Browser session/cookies: `PROFILE/` (local only; never commit).
- Diagnostics on automation failures: `DIAGNOSTICS/` (local only; empty is healthy).
- Immutable-ish file ledger: `ledger.jsonl` (local only).

## Technical sample identity

Technical records are keyed by:

`runUuid :: sampleUuid :: sampleType`

Display sample labels are preserved. A rerun convention such as `MP001--26` remains a separate technical record from `MP001-26`, while `case_family` can link them for biological comparison.

## Collector flow

Daily UI flow:

1. Open browser / login.
2. Scan Runs.
3. Complete latest POOL group.

A POOL may have separate DNA and RNA runs. The same POOL number is treated as one group, while each run remains technically separate.

Readiness rules:

- RUN/sample `COMPLETED`: eligible for Clinical/SNV/CNV/Fusion/QC collection.
- `RUNNING`, `PROCESSING`, `QUEUED`, `SUBMITTED`: WAITING, not an error.
- `ERROR`, `FAILED`, `CANCELED`: blocked/upstream error.
- QC `about:blank`, `The QC report could not be loaded.`, or persistent loading while upstream is not ready: WAITING and retry later.

## Data collected

- Runs/sample metadata and status.
- Clinical variant view with pagination/accordion expansion.
- Full SNV TSV, including low-VAF/filtered calls for artifact and contamination analysis.
- CNV TSV.
- Fusion where available.
- MultiQC plot data via Toolbox → Export → Data → Download Plot Data.
- TMB/MSI/HRD and available depth metadata from Runs.

## Mutation analysis

- Per-sample mutation viewer.
- Global mutation search across all POOLs.
- Oncogenic / Likely Oncogenic filters and ordering.
- Exact-variant distribution across technical samples.
- POOL spread analysis: source-high AF vs recipient-low AF patterns.
- Pairwise multi-variant signatures to prioritize possible cross-sample contamination/artifact review.
- External links to OncoKB and Franklin.

The application must not label contamination as proven solely from these heuristics.

## Patient/block metadata

Case-family level:

- Full name
- National ID
- Age at diagnosis (fixed, not recalculated with calendar years)
- Sex
- Tumor diagnosis
- Requesting physician
- Reporting physician
- Multi-line case note

Technical-sample level:

- Block ID
- Tumor cell percentage
- Technical sample note

Physician fields offer suggestions from previously entered local values.

## Next priorities

1. Integrate QC/read-quality metrics more directly into exact-variant spread scoring.
2. Make rerun comparison explicit: original vs rerun AF/DP/filter concordance.
3. Improve global mutation result drill-down and longitudinal/pool summaries.
4. Add a safe local backup/export workflow for patient data without sending PHI to the public repository.
5. Continue selector maintenance only from de-identified diagnostics.

## Public/private boundary

The public repository must never contain:

- Patient names or national IDs.
- Real sample/run UUIDs.
- Real downloaded TSV/JSON/MultiQC contents.
- `DATA/`, `OUTPUT/`, `PROFILE/`, `ledger.jsonl`.
- Browser cookies/tokens/session profiles.
- Institution/vendor-specific private URLs or credentials.

The supported platform URL is supplied only through ignored `config.local.json`, legacy local `config.json`, or environment variables.
