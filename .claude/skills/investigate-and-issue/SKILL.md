---
name: investigate-and-issue
description: Root-cause-first triage for a reported UI/data symptom — trace to the real mechanism with live data before opening an issue or writing a fix.
---

# investigate-and-issue

Use when the user reports something confusing ("why doesn't X show up", "why is the count wrong") rather than describing a known fix.

## Investigate before concluding
- Reproduce the symptom against live/real data, not just by reading code and guessing. This repo's bugs have consistently turned out to be subtler than the first hypothesis (e.g. a stale allowlist, a name-matching gap, a last-write-wins merge silently dropping which side of a transfer was authoritative).
- Trace all the way to the actual mechanism — which function, which data transformation, which edge case — before writing anything up. "It's probably a matching issue" is not a root cause; the specific function and the specific reason it fails is.
- If the investigation surfaces a genuine design question with more than one reasonable direction (not just a bug with one obvious fix), lay out the options and their tradeoffs and let the user pick, rather than picking for them.

## Open the issue
- Only after root cause is confirmed. Document the precise mechanism in the issue body — this becomes the spec for the fix and for the regression test.
- Then hand off to [[ship-issue]] to implement, test, verify, and PR it.

## Don't
- Don't open an issue that just restates the symptom without the traced cause.
- Don't fix code before the root cause is confirmed with real data — a fix aimed at the wrong layer will pass tests you wrote around the wrong assumption.
