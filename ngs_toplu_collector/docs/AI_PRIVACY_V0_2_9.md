# AI privacy and identity linkage — v0.2.9

- Patient and physician names are masked before AI export. Example: `Metin Ciris` -> `M***n C***s`.
- Turkish national ID (TC) is validated locally and is never exported.
- A checksum-valid local TC is transformed with HMAC-SHA256 and a private local secret into a stable `patient_link_id`; the same patient can therefore be linked across DNA/RNA records with different MP numbers without disclosing the TC.
- If TC is unavailable, local full name + birth date + sex is only a lower-confidence fallback. Masked name or MP similarity alone must not establish identity.
- The HMAC secret is stored under local `DATA/ai_identity_link.key` and is not committed.
- Raw SOURCE_FILES attachments are scanned for known local patient/physician names and validated TC values. A source file with a hit is withheld from the AI bundle and listed in `source_privacy_blocked`.
- AI bundles remain clinical/genomic data and must not be committed to the public repository.
