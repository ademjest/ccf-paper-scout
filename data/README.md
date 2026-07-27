# Venue Data

`ccf_a_venues.json` is a project-specific transformed mapping used to constrain DBLP retrieval to configured CCF-A venues.

## Source

- Upstream: https://github.com/WenyanLiu/CCFrank4dblp
- Pinned commit: `540396b36bfb46b18cfed22bf5c578d73257c4b9`
- Inputs:
  - `data/ccfRankUrl.js`
  - `data/ccfRankFull.js`
  - `data/ccfRankAbbr.js`
- Upstream license: MIT; full notice in `../THIRD_PARTY_NOTICES.md`

## Transformations

1. Parse the three JavaScript mapping objects.
2. Keep entries whose rank is `A`.
3. Infer `conference` or `journal` from the DBLP path.
4. Normalize the DBLP key, abbreviation, full name, rank and project identifier.
5. Preserve aliases separately when multiple upstream paths refer to one canonical DBLP series.

## Important limitations

- This is not an official CCF API or catalog.
- DBLP series keys do not always distinguish main tracks, workshops, findings, demos or historical aliases.
- The runtime check is a whitelist constraint plus DBLP key-prefix verification, not two independent certifications.
- Users should verify ranks and track policy against the latest official CCF catalog and official proceedings.
