# Limitations and Source Strategy

## Current verified channel

The current default source is DBLP, constrained by a derived CCF-A venue mapping and checked against DBLP record-key prefixes. This favors formal, indexed papers and low operational cost, but DBLP indexing can lag and its search API is not a guaranteed complete chronological feed.

## Why no broad new source is enabled by default yet

The immediate personal goal is a trustworthy daily RL/LLM/Agent digest. Adding broad sources before fixing retrieval windows, state and delivery would increase noise and failure modes. The next sources should be evidence-specific adapters rather than a single mixed pool.

Recommended order:

1. **OpenReview accepted papers** for covered AI/ML venues, as an `early-accepted` channel with invitation/status evidence.
2. **Official proceedings/accepted lists** for high-priority venues, as an independent verification source.
3. **OpenAlex ISSN/source-ID journal feed** for CCF-A journals and online-first papers.
4. **arXiv watch channel** only for early topic discovery; never label a preprint CCF-A without acceptance evidence.
5. **Semantic Scholar/Papers with Code enrichment** for citation graph, code and benchmark links after venue eligibility checks.

## Evidence levels

- `verified`: DBLP or official proceedings record under a configured venue policy.
- `early-accepted`: official/OpenReview acceptance evidence, DBLP pending.
- `preprint-watch`: topic-relevant preprint with no CCF-A acceptance claim.

These levels must remain visually separate in reports and state.

## Track policy

A DBLP series key may include historical aliases or non-main tracks. The current prefix check reduces text-search false positives but does not prove main-track status. Future venue records should specify allowed/excluded tracks and independent evidence.

## Recommendation limitations

The default ranker is weighted sparse term matching. It is transparent and cheap but weaker than semantic embeddings for synonyms and implicit topics. LLM focus cards summarize only supplied titles/abstracts and may omit details not present there; they must not be treated as full-paper reviews.
