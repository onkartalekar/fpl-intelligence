---
name: run-full-tests
description: Run this repo's full test suite the standard way and handle the predictable backgrounding/timeout behavior correctly.
---

# run-full-tests

Always run the **full** suite before considering an implementation done — not just the test file for the module you touched. The one narrow exception (a doc-only diff, every changed file `.md`) is defined in [[ship-issue]] step 4, not here — don't invent other exceptions by analogy.

```bash
python3 scripts/run_tests_parallel.py --python /Users/onkartalekar/.local/bin/python3.11
```

- Issue #178: this runs the same four module groups `.github/workflows/tests.yml`'s CI matrix (#175) already runs as separate jobs, as four local subprocesses instead — bounded by the slowest group instead of the full serial sum. Measured on the same machine, same interpreter, as of August 2026 (758 tests): ~193s here vs. ~410s for the equivalent serial command below. Treat any specific number here as a rough, dated order of magnitude, not a threshold — don't conclude a run has hung just because it takes longer than a figure written here.
- **Always pass `--python /Users/onkartalekar/.local/bin/python3.11` explicitly.** The script's own default (`sys.executable`) inherits whatever interpreter ran it, and plain `python3` in this environment resolves to a slow system Python 3.9 — the same interpreter gap that let issue #173's `socket.timeout`/`TimeoutError` bug go undetected in the first place. The script prints its own `WARNING` if the detected interpreter is below 3.10; don't ignore that warning if you see it, fix the `--python` value instead.
- A run is only "done" when the output ends in `All groups passed.` Any `FAIL` needs a fix and a re-run — don't report success on a partial or unread result.
- It still reliably exceeds the 120s foreground `Bash` timeout and will auto-background — that's expected, not a hang. See the blocking-call and `Monitor` guidance below; it applies exactly the same here.

## Fallback: the plain serial command

```bash
PYTHONPATH=src /Users/onkartalekar/.local/bin/python3.11 -m unittest discover -s tests -p "test_*.py"
```

Slower (no parallelism), but useful when you want output for one specific file (`... -m unittest tests.test_foo -v`), or you suspect the parallel script's grouping itself of hiding something and want a from-scratch sanity check. Same interpreter-path rule applies — don't drop back to plain `python3` here either.

## Backgrounding, every time either command is used

- **Run it as a normal blocking call and wait for it to finish in the same turn, rather than backgrounding it and ending your turn to "wait for a notification."** This has been the single most repeated failure mode around this skill: an agent backgrounds the suite, says it will wait for a completion notification, and then simply stalls instead of ever resuming — this recurred across multiple issues in this repo's history. If the command auto-backgrounds anyway because it exceeds the foreground timeout, use `Monitor` with an until-loop polling the output for a terminal `Ran .../OK/FAILED/ERROR` (serial) or `All groups passed.`/`group(s) failed` (parallel) line, rather than ending your turn on the assumption something else will wake you back up.
- Don't poll the output file with repeated `Read` calls in a tight loop while waiting either — `Monitor`'s until-loop (or a single blocking call) is the right tool, not manual polling.
