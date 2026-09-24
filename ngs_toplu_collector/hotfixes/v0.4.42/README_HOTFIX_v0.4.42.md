# v0.4.42 — Hasta/Blok cinsiyet + Enlil demografi + DOB yaş

This directory contains the de-identified source patches for the validated local v0.4.42 update.

## Behavior

- AI clinical-context sex comes first from `Hasta / Blok / Ortak Bilgiler > Cinsiyet`.
- A manually saved local sex is authoritative and is never overwritten by GenNext/Altium or ENLIL.
- ENLIL JAB tries to read an explicit selected `Erkek` / `Kadın` value outside the 16-column pathology result grid. If both options are merely visible or the state is ambiguous, nothing is written.
- If local sex is empty and ENLIL exposes one explicit value, it is written to shared case metadata and the validated-TC patient profile.
- Age is derived from DOB using ENLIL request date, then ENLIL first-accept date, then first NGS run date. Legacy ENLIL age is only a fallback.
- DOB remains local-only and is not exported in `clinical_context`; only derived age and age reference date are exported.
- v0.4.41 Clinical force-refresh, v0.4.40 clinical_context and v0.4.39 exact HRD Score behavior are preserved.

## Files

- `enlil_lookup.py.patch`
- `db.py.patch`
- `metadata_editor.py.patch`
- `ai_export.py.patch`

These patches are relative to the validated local v0.4.41 line.

## Privacy

No patient bundle, TC/national ID, full patient name, local SQLite database, OUTPUT, PROFILE, browser token/cookie, run/sample export or institution-specific private endpoint is included.
