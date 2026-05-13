---
name: skill-creator
version: 1.0.0
description: |
  Create new skills following the GBrain conformance standard. Generates SKILL.md
  with frontmatter, Contract, Phases, Output Format, and Anti-Patterns. Checks
  MECE against existing skills. Updates manifest and resolver.
triggers:
  - "create a skill"
  - "new skill"
  - "improve this skill"
tools:
  - search
  - list_pages
mutating: true
---

# Skill Creator

## Contract

This skill guarantees:
- New skill follows conformance standard (frontmatter + required sections)
- MECE check: no overlap with existing skills' triggers
- Manifest.json updated
- RESOLVER.md updated with routing entry
- Skill passes conformance tests (`bun test test/skills-conformance.test.ts`)

## Phases

1. **Identify the gap.** What capability is missing? What user intent has no skill?
2. **MECE check.** Review `skills/manifest.json` and `skills/RESOLVER.md`. Does any existing skill already cover this? If so, extend it instead of creating a new one.
3. **Create SKILL.md.** Use this template:

```yaml
---
name: {skill-name}
version: 1.0.0
description: |
  {One paragraph describing what the skill does and when to use it.}
triggers:
  - "{trigger phrase 1}"
  - "{trigger phrase 2}"
tools:
  - {tool1}
  - {tool2}
mutating: {true|false}
---

# {Skill Title}

## Contract
{What this skill guarantees — 3-5 bullet points}

## Phases
{Numbered workflow steps}

## Output Format
{What good output looks like}

## When NOT to Use
{Specific cases where this skill is the wrong tool. Name the adjacent skill
or alternative path that should run instead. A skill without explicit
non-use conditions tends to get applied everywhere.}

## Common Failure Modes
{Failures the implementer should expect: bad inputs, brittle assumptions,
integration edges. Cite at least one real incident the skill exists to
prevent — the regression test should map 1:1 to an entry here.}

## Recovery Strategy
{When the normal workflow fails, the rollback or alternate path. If recovery
requires manual intervention, name the artifact the next agent should consult
(log file, audit trail, brain page).}

## Anti-Patterns
{What NOT to do — 3-5 items. Distinct from "When NOT to Use": this is
about mistakes inside the skill, not about choosing the wrong skill.}

## Tools Used
{GBrain operations used, with descriptions}
```

4. **Add to manifest.** Update `skills/manifest.json` with name, path, description.
5. **Add to resolver.** Update `skills/RESOLVER.md` with routing entry in the appropriate category.
6. **Verify.** Run `bun test test/skills-conformance.test.ts` to confirm the new skill passes.

## Output Format

New `skills/{name}/SKILL.md` file + updated manifest + updated resolver.

## When NOT to Use

- The capability is already covered by an existing skill — extend it instead
- The task is a one-off (write a TODO entry or commit comment instead)
- The "skill" would just wrap a single CLI command with no judgment layer

## Common Failure Modes

- Two skills declaring the same trigger — RESOLVER.md routing becomes ambiguous
- A skill marked `writes_pages: true` whose `writes_to:` directories aren't
  in `_brain-filing-rules.json` (filing-audit Check 6 will fail)
- Scaffolded SKILLIFY_STUB markers left in committed scripts
  (check-resolvable --strict will fail)

## Recovery Strategy

If conformance fails after creation: run `gbrain skillpack-check --json` to
get the precise failure list, then resolve each issue (replace stubs, align
triggers, update manifest) before committing. If MECE overlap surfaces
post-merge, the right move is usually MERGE the two skills rather than
delete the new one — overlapping triggers are the symptom.

## Anti-Patterns

- Creating a skill that overlaps with an existing one (violates MECE)
- Skipping the MECE check against existing skills
- Creating a skill without triggers in frontmatter
- Not updating manifest.json and RESOLVER.md
- Creating a skill without an Anti-Patterns section
- Shipping a SKILL.md without When-NOT / Failure-Modes / Recovery sections —
  the conformance ratchet will warn now and fail later
