# Provenance and Independence

CCF Paper Scout is an independent implementation authored for this repository.

## Code provenance

The Python implementation was written independently using public API documentation for Zotero, DBLP, OpenAlex, SMTP, and OpenAI-compatible chat completion interfaces. It is not a fork of `TideDra/zotero-arxiv-daily`, and no source file from that AGPL-3.0 project is redistributed here.

The general workflow “use a Zotero library to personalize paper discovery” is an idea and engineering pattern seen in several public projects; conceptual acknowledgement is provided in `THIRD_PARTY_NOTICES.md` and `README.md`.

## Data provenance

The CCF venue mapping is a transformed subset of `WenyanLiu/CCFrank4dblp` at pinned commit `540396b36bfb46b18cfed22bf5c578d73257c4b9`. See `data/README.md` and `THIRD_PARTY_NOTICES.md`.

## No official status

This repository is not affiliated with or endorsed by the China Computer Federation, Zotero, DBLP, OpenAlex, OpenReview, any conference, or any publisher. Venue rank is an eligibility/reference signal and not an assessment of an individual paper's scientific quality.
