# Third-Party Notices

This file records third-party code/data that is redistributed or materially derived in CCF Paper Scout. Conceptual inspirations are listed separately and do not imply code reuse.

## CCFrank4dblp venue metadata

`src/ccf_paper_scout/data/ccf_a_venues.json` contains a transformed subset of venue metadata derived from:

- Project: `WenyanLiu/CCFrank4dblp`
- Repository: https://github.com/WenyanLiu/CCFrank4dblp
- Pinned upstream commit: `540396b36bfb46b18cfed22bf5c578d73257c4b9`
- Upstream files: `data/ccfRankUrl.js`, `data/ccfRankFull.js`, `data/ccfRankAbbr.js`
- License: MIT
- Copyright: Copyright (c) 2019-2023 WenyanLiu

The upstream MIT license notice follows:

> MIT License
>
> Copyright (c) 2019-2023 WenyanLiu (https://github.com/WenyanLiu/CCFrank4dblp)
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

Transformations performed by this project include filtering to rank A, normalizing fields, and adding project-specific identifiers. The derived mapping is not an official CCF dataset or API and may contain delays or mistakes; users must verify against the latest official CCF catalog.

## Conceptual acknowledgements (no redistributed code)

The independent implementation was informed by publicly documented ideas from:

- `TideDra/zotero-arxiv-daily` — Zotero-personalized paper discovery and automated delivery: https://github.com/TideDra/zotero-arxiv-daily
- `ccfddl/ccf-deadlines` — machine-readable venue/deadline organization: https://github.com/ccfddl/ccf-deadlines
- `yuandong-tian/arXiv_recbot` — explicit feedback workflow inspiration: https://github.com/yuandong-tian/arXiv_recbot

No affiliation or endorsement is implied. These projects' code is not redistributed by this repository unless a future notice explicitly says otherwise.
