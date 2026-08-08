---
name: run-full-tests
description: Run this repo's full test suite the standard way and handle the predictable backgrounding/timeout behavior correctly.
---

# run-full-tests

Always run the **full** suite before considering an implementation done — not just the test file for the module you touched.

```bash
PYTHONPATH=src /Users/onkartalekar/.local/bin/python3.11 -m unittest discover -s tests -p "test_*.py"
```

- The suite takes roughly 130-145 seconds, which reliably exceeds the 120s foreground `Bash` timeout — it will auto-background. This is expected, not a hang.
- Don't poll the output file with repeated `Read` calls while waiting. Let the background task finish and rely on the task-completion notification, then read the result once.
- A run is only "done" when the output ends in `OK`. Any `FAILED`/`ERROR` needs a fix and a re-run — don't report success on a partial or unread result.
