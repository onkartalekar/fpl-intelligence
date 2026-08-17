"""Data ingestion and normalization: official FPL data, scraped transfer sources, and the
shared plumbing that turns raw upstream payloads into this app's own shapes.

`fpl_data.py` (official FPL API), `pl_transfers.py` (Premier League transfer centre),
`catalog.py` (players/fixtures normalization), `manager_data.py` (public manager lookups),
`transfers.py` (confirmed-transfer normalization), `relevance.py` (FPL-relevance/club-impact
enrichment), `news_signals.py` (gated LLM signal extraction), `deadline_windows.py` (gameweek-
deadline arithmetic). None of these import anything else from this package -- they're the leaves
`refresh.py` and `modeling/` build on top of.
"""
