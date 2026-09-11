# Privacy and data boundary

This public repository contains source code only.

The application is designed so that patient/specimen metadata and sequencing exports remain on the user's computer. Local fields can include names, national identifiers, diagnoses, physicians, block IDs and tumor percentages; these values must not be committed to this repository.

Repository ignore rules exclude the normal local data, output, browser-profile and diagnostic paths. Contributors must still review every commit before publishing because diagnostic HTML/screenshots or manually moved files may contain identifying information.

Institution-specific platform addresses and credentials belong only in `config.local.json` or environment variables and are not part of the public source tree.
