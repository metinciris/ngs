# NGS Mutasyon Havuzu

A local Python desktop application for NGS run collection, mutation review, technical rerun comparison and POOL-level variant spread analysis.

> Public-source edition: institution/vendor-specific endpoints, credentials, patient data and sequencing exports are intentionally excluded.

## Features

- Playwright-based browser collection with a persistent local Chromium profile.
- Runs discovery and DNA/RNA POOL grouping.
- Upstream readiness awareness: incomplete runs stay **WAITING** instead of being misclassified as collector failures.
- Clinical/SNV/CNV/Fusion/QC collection when available.
- Full SNV retention, including filtered/low-VAF calls, for artifact/contamination review.
- Mutation viewer and archive-wide mutation search.
- Oncogenic / Likely Oncogenic filtering.
- POOL spread and sample-to-sample shared-variant analysis.
- Technical reruns remain separate while a case-family link supports comparison.
- Local patient/block metadata with physician autocomplete.
- OncoKB and Franklin browser links.

## Install

Requirements: Python 3.10+ and Windows for the included `.bat` launchers.

1. Copy `config.example.json` to `config.local.json`.
2. Set your private analysis-platform `base_url` and `runs_url` in `config.local.json`.
3. Run `KURULUM.bat` once.
4. Run `BASLAT.bat`.

You may alternatively set `NGS_PLATFORM_BASE_URL` and `NGS_PLATFORM_RUNS_URL` environment variables.

## Daily workflow

1. **Tarayıcıyı Aç / Login**
2. **RUNS'u Tara**
3. **Son POOL'u Tamamla**

If a run/sample is not complete, collection waits and can be retried after a later Runs scan. QC pages that are temporarily blank/unavailable are treated as waiting when upstream processing is incomplete.

## Data safety

The following are local-only and git-ignored:

- `DATA/` — SQLite state/index and patient metadata
- `OUTPUT/` — downloaded analysis data
- `PROFILE/` — Chromium cookies/session
- `DIAGNOSTICS/` — error snapshots (empty is healthy)
- `ledger.jsonl`
- `config.local.json`

Do not publish patient identifiers, real run/sample UUIDs, raw sequencing outputs or browser profiles.

## Continue after losing the local project/chat

Read **[PROJECT_STATE.md](PROJECT_STATE.md)** and **[RECOVERY.md](RECOVERY.md)**. They are deliberately maintained so development can resume from GitHub without relying on a specific chat history.

## Current version

Public source: **v0.2.4**.
