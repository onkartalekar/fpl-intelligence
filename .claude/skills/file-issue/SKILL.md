---
name: file-issue
description: Write a new GitHub issue capturing a design/architecture gap surfaced through code investigation or conversation, matching this repo's established issue house style (evidence-cited Context/Request/Not in scope/Dependency, amended in place as direction evolves). Not for a reported bug symptom (use investigate-and-issue) or for research questions on an already-existing issue (use plan-issue).
---

# file-issue

Use when a conversation — a user's question, your own investigation, a code read prompted by something else — surfaces a real, code-grounded gap or follow-up worth tracking as its own issue. Not a reported symptom (that's [[investigate-and-issue]]) and not a restatement of something an existing open issue already covers.

## 1. Ground it in real code first

- Don't file from a hunch or from memory of how the code "probably" works. Read the actual code paths and cite exact `file.py:line` locations for the claims in the issue body — this repo's issues consistently do this (e.g. issue #102 cites `refresh.py:180-182`, `server.py:97`, `model_performance.py`'s exact key format) rather than describing behavior abstractly.
- If the gap only becomes clear by tracing through *why* something can't be solved a simpler way (e.g. "why this can't be backfilled retroactively"), write that reasoning out explicitly as its own section. It's often the most valuable part of the issue for whoever picks it up later — it's exactly the insight that stops a future implementer from re-deriving it, or missing it and building the wrong thing.

## 2. Structure the body

Match this repo's established shape (see issues #78-#83, #101, #102):

- `## Context` — what prompted this, citing the real code as it exists today.
- A reasoning section when the "why" isn't obvious from Context alone, e.g. `## Why this can't be retrofitted later, and has to be built forward-only` (issue #102). Skip it if Context already carries the reasoning.
- `## Request` — the concrete ask, broken into sub-bullets. Mark genuinely open design questions as explicitly open rather than silently deciding one yourself (issue #102 originally left "one snapshot or per-checkpoint" as an open `(a)`/`(b)` choice until the user picked) — it's not your call to make unless asked.
- `## Not in scope` — name adjacent things this issue deliberately does *not* cover, especially anything an implementer might reasonably assume is included (issue #102 explicitly ruled out #65's ML model and the actual-outcome/`manager_picks` half, so nobody builds those thinking they're part of this ask).
- `## Dependency` — name the specific blocking issue(s), or state "None remaining" if genuinely ready to build.

## 3. Amend in place, don't refile, when direction changes

If the user gives new direction after filing (a decision on an open question, a scope change), edit the existing issue rather than opening a new one:

```bash
gh issue view <N> --json body -q .body > /tmp/issue-<N>-current.md
# edit the file
gh issue edit <N> --body-file /tmp/issue-<N>-current.md
```

Prefix the changed bullet with `**Decided (<date>): ...**` or `**Superseded (<date>): ...**` rather than silently rewriting it, so the issue body keeps a readable trail of how the design evolved (see issues #83 and #102, both amended this way more than once). Always fetch the current body first and edit that — don't reconstruct it from memory, you will drift from what's actually posted.

## 4. File it

`gh issue create --title "..." --body-file <path>` — write the body to a scratch file first rather than inlining a long `--body` string.

## 5. Stop

Filing the issue is the deliverable. Don't start implementing and don't switch to [[plan-issue]] or [[ship-issue]] unless the user separately asks for that — this skill only produces the issue.
