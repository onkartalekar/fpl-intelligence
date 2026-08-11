---
name: run-full-tests
description: Run this repo's full test suite the standard way and handle the predictable backgrounding/timeout behavior correctly.
---

# run-full-tests

Always run the **full** suite before considering an implementation done — not just the test file for the module you touched. The one narrow exception (a doc-only diff, every changed file `.md`) is defined in [[ship-issue]] step 4, not here — don't invent other exceptions by analogy.

```bash
PYTHONPATH=src /Users/onkartalekar/.local/bin/python3.11 -m unittest discover -s tests -p "test_*.py"
```

- Use this exact interpreter path, not plain `python3` — plain `python3` in this environment resolves to a slow system Python 3.9 and can make the suite take 6+ minutes or look hung when it isn't.
- The suite keeps growing. As of August 2026 it's ~450 tests, taking roughly 330-380 seconds. Treat any specific number here as a rough, dated order of magnitude, not a threshold — don't conclude a run has hung just because it takes longer than a figure written here. It reliably exceeds the 120s foreground `Bash` timeout and will auto-background — that's expected, not a hang.
- **Run it as a normal blocking call and wait for it to finish in the same turn, rather than backgrounding it and ending your turn to "wait for a notification."** This has been the single most repeated failure mode around this skill: an agent backgrounds the suite, says it will wait for a completion notification, and then simply stalls instead of ever resuming — this recurred across multiple issues in this repo's history. If the command auto-backgrounds anyway because it exceeds the foreground timeout, use `Monitor` with an until-loop polling the output for a terminal `Ran .../OK/FAILED/ERROR` line, rather than ending your turn on the assumption something else will wake you back up.
- Don't poll the output file with repeated `Read` calls in a tight loop while waiting either — `Monitor`'s until-loop (or a single blocking call) is the right tool, not manual polling.
- A run is only "done" when the output ends in `OK`. Any `FAILED`/`ERROR` needs a fix and a re-run — don't report success on a partial or unread result.
