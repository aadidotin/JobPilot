# TODOS

## Interview prep pack (deferred from CEO review, 2026-07-19)
- **What:** When an application is logged as "interview," auto-generate a prep doc: archived JD (JD-archiver output), company snapshot from diff data (hiring velocity, open roles), likely questions derived from JD requirements, and which base-resume-YAML stories map to each requirement. One Sonnet call, markdown to Telegram.
- **Why:** Interviews are the bottleneck metric; front-loads ~1h of manual prep per interview.
- **Pros:** Fires rarely (pennies); all input data already exists once the JD archiver ships.
- **Cons:** Zero value until the first interview lands — which is why it's deferred, not scoped.
- **Context:** CEO plan: `~/.gstack/projects/JobPilot/ceo-plans/2026-07-19-jobpilot-post-send.md`. Decision D3.4: deferred; siblings (follow-ups, referrals, archiver, skill gaps, Sunday digest) were added to scope.
- **Depends on / blocked by:** JD archiver (in scope, weekend 3) and outcome logging (weekend 4). Build when the first interview is logged.

## `jobpilot validate` — companies.yaml curation-time validator
- **What:** CLI command that checks every board slug in companies.yaml (fetch, parse, >0 jobs), verifies alias lists don't collide across companies, and prints a red/green table.
- **Why:** The weekly companies.yaml curation habit is the moat, and PROJECT.md §11 lists "companies.yaml validated weekly" as a risk mitigation — but nothing in the plan builds the validator. Slug rot is the #1 adapter failure mode; this catches it at curation time (the E11 per-tier silence alert catches it at runtime).
- **Pros:** Makes the weekly habit a 30-second check; catches migrated board slugs before they silently blind a source.
- **Cons:** One more CLI surface; partially overlaps the E11 runtime alert.
- **Context (for future pickup):** Design doc: `~/.gstack/projects/JobPilot/atreus-unknown-design-20260719-180727.md` (Amendments E5, E11). The validator should reuse the PollResult adapters — it is essentially "run every adapter once, report success/error per board" plus alias-collision checks. ~20 min with AI pairing once adapters exist.
- **Depends on / blocked by:** T2 (PollResult adapter contract + ATS adapters) from the eng-review task list. Target: weekend 2+.
