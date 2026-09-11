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
- POOL-level **AI / Reporting package** export with explicit DNA/RNA scope, TMB/MSI/HRD, MultiQC TSV/CSV/JSON parsing, structured CNV, enriched SNV annotations, all-call technical fingerprinting and optional raw source attachments.

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

## AI / Reporting package

The **YZ / Raporlama** tab generates one local bundle for a selected POOL group and an explicit scope: **DNA** (default), RNA, or DNA+RNA. DNA-only requests do not include the matching RNA run.

The bundle contains:

- POOL run states and Oncogenic/Likely Oncogenic exact shared/high→low/multi-variant signals.
- A second **all-SNV/indel technical fingerprint** that intentionally includes benign, unknown and non-PASS calls to expose cross-sample density, high→low carry-over patterns and recurrent platform artifacts. This is not BAM read-level identity testing.
- Patient/case diagnosis, age at diagnosis, sex, block and tumor percentage, with missing-context warnings instead of invented values.
- TMB, MSI and HRD captured from rendered Runs sample rows. A later Runs scan can backfill these fields into existing records.
- MultiQC **TSV/CSV/JSON Data exports**, including General Stats, VerifyBAMID/FREEMIX-compatible contamination field, Picard duplication and other exported tables. Full mode embeds parsed tables, not merely the QC ZIP filename.
- All indexed **Oncogenic** and **Likely Oncogenic** calls, including non-PASS calls, enriched from the raw SNV table with transcript/cDNA/protein, CancerVar, ClinVar, COSMIC and available levels.
- Structured CNV output with gene/event/fold change/probe/level fields when present, while true no-data/missing states stay explicit.
- Clinical summary/raw JSON and RNA/Fusion data when the selected scope requires them.
- `SOURCE_INVENTORY.json` plus optional `SOURCE_FILES/` attachments containing the actual Clinical/SNV/CNV/QC/Fusion exports for AI-side verification.
- `POOL_AI_PROMPT.md`, compact/full JSON and a SHA256 manifest with package-completeness status.

Direct identifiers (name and national ID) are **off by default**. Generated AI bundles are patient/NGS data and must never be committed to the public repository. Shared/high→low/fingerprint flags are review signals, not an automatic contamination diagnosis or a final clinical interpretation.

## Data safety

The following are local-only and git-ignored:

- `DATA/` — SQLite state/index and patient metadata
- `OUTPUT/` — downloaded analysis data
- `AI_EXPORT/` — generated patient-level AI/reporting bundles
- `PROFILE/` — Chromium cookies/session
- `DIAGNOSTICS/` — error snapshots (empty is healthy)
- `ledger.jsonl`
- `config.local.json`

Do not publish patient identifiers, real run/sample UUIDs, raw sequencing outputs or browser profiles.

## Continue after losing the local project/chat

Read **[PROJECT_STATE.md](PROJECT_STATE.md)** and **[RECOVERY.md](RECOVERY.md)**. They are deliberately maintained so development can resume from GitHub without relying on a specific chat history.

## Current version

Public source: **v0.2.6**.
