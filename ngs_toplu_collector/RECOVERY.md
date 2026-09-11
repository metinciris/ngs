# Recovery / continue development

This repository is the canonical **source-code and project-state backup**. It is deliberately not a patient-data backup.

## If the local application source is deleted

1. Clone/download this repository.
2. Enter `ngs_toplu_collector/`.
3. Copy `config.example.json` to `config.local.json`.
4. Put the institution's private Runs URL/base URL in `config.local.json`.
5. Run `KURULUM.bat`, then `BASLAT.bat`.
6. Read `PROJECT_STATE.md` before further development.

## If the development chat is deleted

Give the next developer/assistant this repository and say:

> Continue the `ngs_toplu_collector` project from `PROJECT_STATE.md`. Preserve the public/private data boundary and technical rerun identity.

That document records the current architecture, completed behavior, decisions and next priorities.

## If local patient/run data is deleted

The public GitHub repository **cannot restore it** because PHI and sequencing exports are intentionally excluded.

Back up these local items separately to an institution-approved encrypted destination:

- `DATA/`
- `OUTPUT/`
- optionally `ledger.jsonl`

`PROFILE/` contains browser session material and should normally be recreated by logging in again rather than copied to an untrusted backup.

Never upload a patient-data backup to this public repository.
