# Agent instructions

## Project rules
- Prefer correctness over cleverness.
- When refactoring: keep behavior identical unless explicitly told otherwise.
- Keep changes small and reviewable; avoid giant unrelated diffs.

## Python standards
- Target Python 3.11+ (adjust if needed).
- Use type hints for public APIs.
- Add/extend tests for every bug fix and major feature.
- Run: pytest -q (or update this to your real test command).
- If formatting is present: respect existing formatter (black/ruff/etc).

## When unsure
- Inspect the repo and propose a plan before changing many files.
- If an assumption affects behavior, call it out and pick the safest default.


## Project Specific
- Read PROJECT_SPEC.md first and treat it as the source of truth.
- Start by writing a short plan:
  - identify feasibility risks (especially “hive out” writing)
  - propose the best technical approach and justify it
  - define an MVP milestone and follow-on milestones
- Then implement incrementally:
  - keep diffs small and reviewable
  - add tests for the core logic early (even if GUI tests are minimal)
  - run tests after meaningful changes and fix failures before continuing
- Prefer correctness and robustness over speed of delivery.
- Never overwrite the input hive; always export to a new file path.
- If there are multiple viable toolkits/libraries, pick one and proceed, but explain why.
