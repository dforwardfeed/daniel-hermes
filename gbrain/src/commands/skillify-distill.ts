/**
 * gbrain skillify distill — opt-in post-task curation.
 *
 * Reads a friction log (by run-id) plus a one-line task description, asks the
 * chat model to propose ONE curation decision against the current skill
 * library, and emits a strict JSON envelope.
 *
 * Output shape (single JSON object on stdout):
 *
 *   {
 *     decision: 'insert' | 'update' | 'merge' | 'delete' | 'nothing',
 *     confidence: 'high' | 'medium' | 'low',
 *     rationale: string,
 *     target_skills: string[],
 *     change_payload: {
 *       skill_name?: string,
 *       skill_markdown?: string,
 *       merged_into?: string,
 *       deletion_reason?: string,
 *     }
 *   }
 *
 * The command NEVER writes to the skills directory. The human or agent reviews
 * the proposal and applies it via `gbrain skillify scaffold` (insert), a manual
 * edit (update), `mv + delete + edit` (merge), or `rm + RESOLVER.md edit`
 * (delete). The distill is advisory; conformance + check-resolvable + cross-modal
 * remain the gates on what actually lands.
 *
 * Why one command, not ten prompts: every CRUD action shares the same input
 * (task + friction + library state) and the same output shape. Splitting into
 * separate `insert-prompt` / `update-prompt` / `merge-prompt` commands would
 * force the caller to know the decision BEFORE asking for it — defeating the
 * point of consulting a model. The model's first job IS the decision; the
 * payload comes from the same call.
 */

import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join, resolve as resolvePath } from 'node:path';
import { activeRunId, readFriction, type FrictionEntry } from '../core/friction.ts';
import { loadConfig } from '../core/config.ts';
import { configureGateway, chat, isAvailable } from '../core/ai/gateway.ts';
import { autoDetectSkillsDir } from '../core/repo-root.ts';

// ---------------------------------------------------------------------------
// Argv parsing
// ---------------------------------------------------------------------------

interface DistillFlags {
  help: boolean;
  json: boolean;
  task: string | null;
  runId: string | null;
  fromFile: string | null;
  skillsDir: string | null;
  model: string | null;
  /** Max friction entries to include in the prompt (avoids prompt bloat). */
  maxFriction: number;
}

const HELP = `gbrain skillify distill --task "<description>" [options]

Propose ONE curation decision (insert | update | merge | delete | nothing)
against the current skill library, given a task description plus the
friction log captured while doing the task. Output is JSON on stdout.

Required:
  --task "..."           one-line description of what was attempted

Friction source (pick one; defaults to the active run-id):
  --run-id <id>          read friction from $GBRAIN_HOME/friction/<id>.jsonl
  --from-file <path>     read JSONL from an explicit path
  (default)              use $GBRAIN_FRICTION_RUN_ID or "standalone"

Options:
  --skills-dir <path>    override auto-detected skills/ directory
  --model <id>           chat model override (e.g. "anthropic:claude-opus-4-7")
  --max-friction <n>     cap number of friction entries sent to the model (default 40)
  --json                 force machine-readable output (default when piped)
  --help

The command is opt-in and advisory. It never edits skills/ or RESOLVER.md.
After reviewing the proposal, apply it via:
  - INSERT  → gbrain skillify scaffold <name> --description "..." --triggers "..."
  - UPDATE  → manually edit skills/<name>/SKILL.md
  - MERGE   → fold both skills' content into one, remove the other, edit RESOLVER.md
  - DELETE  → rm -rf skills/<name>/, remove its row from RESOLVER.md + manifest.json
  - NOTHING → no action; the friction was a one-off
`;

function parseArgs(argv: string[]): DistillFlags {
  const f: DistillFlags = {
    help: false,
    json: false,
    task: null,
    runId: null,
    fromFile: null,
    skillsDir: null,
    model: null,
    maxFriction: 40,
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--help' || a === '-h') f.help = true;
    else if (a === '--json') f.json = true;
    else if (a === '--task') { f.task = argv[i + 1] ?? null; i++; }
    else if (a?.startsWith('--task=')) f.task = a.slice('--task='.length) || null;
    else if (a === '--run-id') { f.runId = argv[i + 1] ?? null; i++; }
    else if (a?.startsWith('--run-id=')) f.runId = a.slice('--run-id='.length) || null;
    else if (a === '--from-file') { f.fromFile = argv[i + 1] ?? null; i++; }
    else if (a?.startsWith('--from-file=')) f.fromFile = a.slice('--from-file='.length) || null;
    else if (a === '--skills-dir') { f.skillsDir = argv[i + 1] ?? null; i++; }
    else if (a?.startsWith('--skills-dir=')) f.skillsDir = a.slice('--skills-dir='.length) || null;
    else if (a === '--model') { f.model = argv[i + 1] ?? null; i++; }
    else if (a?.startsWith('--model=')) f.model = a.slice('--model='.length) || null;
    else if (a === '--max-friction') {
      const n = Number.parseInt(argv[i + 1] ?? '', 10);
      if (Number.isFinite(n) && n > 0) f.maxFriction = n;
      i++;
    }
  }
  return f;
}

// ---------------------------------------------------------------------------
// Library introspection
// ---------------------------------------------------------------------------

interface SkillSummary {
  name: string;
  description: string;
  triggers: string[];
  path: string;
}

/**
 * List every skills/<dir>/SKILL.md and parse its frontmatter into a summary.
 * Deliberately keeps the parse tiny (regex-only); the existing
 * skills-conformance test does the heavy lifting.
 */
function listSkills(skillsDir: string): SkillSummary[] {
  if (!existsSync(skillsDir)) return [];
  const out: SkillSummary[] = [];
  for (const entry of readdirSync(skillsDir, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const skillPath = join(skillsDir, entry.name, 'SKILL.md');
    if (!existsSync(skillPath)) continue;
    let raw = '';
    try { raw = readFileSync(skillPath, 'utf-8'); } catch { continue; }
    const fmMatch = raw.match(/^---\n([\s\S]*?)\n---/);
    if (!fmMatch) continue;
    const fm = fmMatch[1];
    const nameLine = fm.match(/^name:\s*(.+)$/m);
    const descLine = fm.match(/^description:\s*(.+)$/m);
    const triggerLines = [...fm.matchAll(/^\s+-\s*"([^"]+)"$/gm)].map(m => m[1]);
    out.push({
      name: nameLine?.[1].trim() || entry.name,
      description: descLine?.[1].trim() || '',
      triggers: triggerLines,
      path: `skills/${entry.name}/SKILL.md`,
    });
  }
  return out.sort((a, b) => a.name.localeCompare(b.name));
}

// ---------------------------------------------------------------------------
// Friction loading
// ---------------------------------------------------------------------------

/**
 * Trim a friction entry down to what the model needs. Drops cwd, version,
 * source — those bloat the prompt without informing curation. Keeps the
 * fields that actually describe what went wrong.
 */
function compactFriction(e: FrictionEntry): Record<string, unknown> {
  const out: Record<string, unknown> = {
    ts: e.ts,
    phase: e.phase,
    kind: e.kind,
    message: e.message,
  };
  if (e.severity) out.severity = e.severity;
  if (e.hint) out.hint = e.hint;
  if (e.class) out.class = e.class;
  if (e.code) out.code = e.code;
  return out;
}

function loadFromFile(path: string): FrictionEntry[] {
  const raw = readFileSync(path, 'utf-8');
  const out: FrictionEntry[] = [];
  for (const line of raw.split('\n')) {
    if (!line.trim()) continue;
    try {
      const parsed = JSON.parse(line);
      // Light shape check — not strict, just enough that compactFriction won't crash.
      if (parsed && typeof parsed === 'object' && typeof parsed.message === 'string') {
        out.push(parsed as FrictionEntry);
      }
    } catch { /* skip malformed line */ }
  }
  return out;
}

// ---------------------------------------------------------------------------
// Prompt
// ---------------------------------------------------------------------------

const DISTILL_SYSTEM = `You are the curator of a skill library for the gbrain agent system.

Your job: given a task description, the friction log captured while doing the task, and the current skill library, decide ONE action:

  insert  — a NEW reusable skill is warranted (a real recurring failure pattern emerged)
  update  — an existing skill is close but needs a sharper rule, missing condition, or recovery path
  merge   — two or more existing skills overlap and should fold into one
  delete  — an existing skill is wrong, redundant, or has been superseded
  nothing — the friction was one-off; no library change is warranted

Decision discipline:
1. Default to "nothing". The library is intentionally compact; do not inflate it on speculation.
2. Insert only when (a) the friction reveals a failure pattern likely to recur, AND (b) no existing skill addresses it. Cite the specific friction entries that justify the new skill in your rationale.
3. Update wins over insert when an existing skill is structurally right but procedurally incomplete.
4. Merge requires both skills to share a trigger phrase OR cover the same underlying procedure. Surface-level topic overlap is NOT enough.
5. Delete requires evidence the skill is wrong (later experience contradicts it) or redundant (a better skill covers everything it covers). Lack-of-use alone is not enough — use the skill-usage capture for that signal.
6. Conservative bias: if you are uncertain, return "nothing" with confidence "low" and explain what evidence would be needed to act.

Output shape — strict JSON, one object, nothing else:

{
  "decision": "insert" | "update" | "merge" | "delete" | "nothing",
  "confidence": "high" | "medium" | "low",
  "rationale": "Two to four sentences citing specific friction entries and library state.",
  "target_skills": ["existing-skill-name", ...],
  "change_payload": {
    "skill_name": "kebab-case-slug (insert only)",
    "skill_markdown": "full SKILL.md body in the shape below (insert/update only)",
    "merged_into": "kebab-case-slug (merge only — which skill survives)",
    "deletion_reason": "one-sentence justification (delete only)"
  }
}

Skill markdown format (use exactly this section order):

---
name: <slug>
version: 0.1.0
description: <one-line>
triggers:
  - "<trigger phrase 1>"
  - "<trigger phrase 2>"
---

# <Title>

## The rule
<the hard rule that prevents recurrence of the failure>

## How to use
<numbered steps>

## When NOT to Use
<adjacent skills or alternative paths that should run instead>

## Common Failure Modes
<bad inputs, brittle assumptions, integration edges — cite at least one real incident>

## Recovery Strategy
<rollback or alternate path when the normal workflow fails>

Do not include surrounding prose or markdown fences. Output ONLY the JSON object.`;

function buildUserPrompt(opts: {
  task: string;
  friction: FrictionEntry[];
  skills: SkillSummary[];
  maxFriction: number;
}): string {
  const truncated = opts.friction.slice(0, opts.maxFriction).map(compactFriction);
  const dropped = Math.max(0, opts.friction.length - opts.maxFriction);
  const skillsBrief = opts.skills.map(s => ({
    name: s.name,
    description: s.description.slice(0, 160),
    triggers: s.triggers,
  }));
  const payload = {
    task: opts.task,
    friction_entries: truncated,
    friction_entries_dropped: dropped,
    current_skills: skillsBrief,
  };
  return JSON.stringify(payload, null, 2);
}

// ---------------------------------------------------------------------------
// Output parsing
// ---------------------------------------------------------------------------

interface DistillProposal {
  decision: 'insert' | 'update' | 'merge' | 'delete' | 'nothing';
  confidence: 'high' | 'medium' | 'low';
  rationale: string;
  target_skills: string[];
  change_payload: {
    skill_name?: string;
    skill_markdown?: string;
    merged_into?: string;
    deletion_reason?: string;
  };
}

const ALLOWED_DECISIONS = new Set(['insert', 'update', 'merge', 'delete', 'nothing']);
const ALLOWED_CONFIDENCE = new Set(['high', 'medium', 'low']);

function parseProposal(text: string): { ok: true; proposal: DistillProposal } | { ok: false; error: string } {
  let body = text.trim();
  // Strip ```json fences if the model added them despite instructions.
  const fenced = /^```(?:json)?\s*\n?([\s\S]*?)\n?```\s*$/i.exec(body);
  if (fenced) body = fenced[1].trim();
  // Find first { ... last }.
  const first = body.indexOf('{');
  const last = body.lastIndexOf('}');
  if (first < 0 || last < first) return { ok: false, error: 'no JSON object in model output' };
  body = body.slice(first, last + 1);
  let parsed: unknown;
  try { parsed = JSON.parse(body); } catch (e) {
    return { ok: false, error: `JSON parse failed: ${(e as Error).message}` };
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { ok: false, error: 'top-level value is not an object' };
  }
  const p = parsed as Record<string, unknown>;
  if (typeof p.decision !== 'string' || !ALLOWED_DECISIONS.has(p.decision)) {
    return { ok: false, error: `decision must be one of ${[...ALLOWED_DECISIONS].join('|')}` };
  }
  if (typeof p.confidence !== 'string' || !ALLOWED_CONFIDENCE.has(p.confidence)) {
    return { ok: false, error: `confidence must be one of ${[...ALLOWED_CONFIDENCE].join('|')}` };
  }
  if (typeof p.rationale !== 'string' || !p.rationale.trim()) {
    return { ok: false, error: 'rationale must be a non-empty string' };
  }
  const targets = Array.isArray(p.target_skills) ? p.target_skills.filter(t => typeof t === 'string') as string[] : [];
  const payload = (p.change_payload && typeof p.change_payload === 'object') ? p.change_payload as Record<string, unknown> : {};
  return {
    ok: true,
    proposal: {
      decision: p.decision as DistillProposal['decision'],
      confidence: p.confidence as DistillProposal['confidence'],
      rationale: p.rationale,
      target_skills: targets,
      change_payload: {
        skill_name: typeof payload.skill_name === 'string' ? payload.skill_name : undefined,
        skill_markdown: typeof payload.skill_markdown === 'string' ? payload.skill_markdown : undefined,
        merged_into: typeof payload.merged_into === 'string' ? payload.merged_into : undefined,
        deletion_reason: typeof payload.deletion_reason === 'string' ? payload.deletion_reason : undefined,
      },
    },
  };
}

// ---------------------------------------------------------------------------
// Gateway bootstrap (mirror of eval-cross-modal's configureGatewayForCli)
// ---------------------------------------------------------------------------

function configureGatewayForCli(): void {
  const config = loadConfig();
  if (!config) {
    configureGateway({
      embedding_model: undefined,
      embedding_dimensions: undefined,
      expansion_model: undefined,
      chat_model: undefined,
      chat_fallback_chain: undefined,
      base_urls: undefined,
      env: { ...process.env },
    });
    return;
  }
  configureGateway({
    embedding_model: config.embedding_model,
    embedding_dimensions: config.embedding_dimensions,
    expansion_model: config.expansion_model,
    chat_model: config.chat_model,
    chat_fallback_chain: config.chat_fallback_chain,
    base_urls: config.provider_base_urls,
    env: { ...process.env },
  });
}

// ---------------------------------------------------------------------------
// Human formatter (when not --json)
// ---------------------------------------------------------------------------

function renderHuman(p: DistillProposal): string {
  const lines: string[] = [];
  lines.push(`Decision:   ${p.decision.toUpperCase()}  (confidence: ${p.confidence})`);
  lines.push('');
  lines.push('Rationale:');
  for (const para of p.rationale.split('\n')) lines.push(`  ${para}`);
  if (p.target_skills.length > 0) {
    lines.push('');
    lines.push(`Target skills: ${p.target_skills.join(', ')}`);
  }
  if (p.decision === 'insert' || p.decision === 'update') {
    if (p.change_payload.skill_name) {
      lines.push('');
      lines.push(`Skill name: ${p.change_payload.skill_name}`);
    }
    if (p.change_payload.skill_markdown) {
      lines.push('');
      lines.push('--- proposed SKILL.md ---');
      lines.push(p.change_payload.skill_markdown.trim());
      lines.push('--- end ---');
    }
  } else if (p.decision === 'merge' && p.change_payload.merged_into) {
    lines.push('');
    lines.push(`Merge survivor: ${p.change_payload.merged_into}`);
  } else if (p.decision === 'delete' && p.change_payload.deletion_reason) {
    lines.push('');
    lines.push(`Deletion reason: ${p.change_payload.deletion_reason}`);
  }
  lines.push('');
  lines.push('This proposal is advisory. Review, then apply manually via the steps in `gbrain skillify distill --help`.');
  return lines.join('\n');
}

// ---------------------------------------------------------------------------
// Main entrypoint
// ---------------------------------------------------------------------------

export async function runSkillifyDistill(args: string[]): Promise<void> {
  const flags = parseArgs(args);
  if (flags.help) {
    process.stdout.write(HELP);
    process.exit(0);
  }
  if (!flags.task) {
    process.stderr.write('Error: --task "<description>" is required\n\n');
    process.stderr.write(HELP);
    process.exit(2);
  }

  // Resolve skills directory.
  let skillsDir: string | null = null;
  if (flags.skillsDir) {
    skillsDir = resolvePath(process.cwd(), flags.skillsDir);
  } else {
    skillsDir = autoDetectSkillsDir().dir;
  }
  if (!skillsDir || !existsSync(skillsDir)) {
    process.stderr.write(
      'Error: could not locate skills/ directory. Pass --skills-dir or run from a repo with skills/RESOLVER.md.\n',
    );
    process.exit(2);
  }

  // Load friction entries.
  let friction: FrictionEntry[] = [];
  let frictionSource = '';
  if (flags.fromFile) {
    if (!existsSync(flags.fromFile)) {
      process.stderr.write(`Error: --from-file path not found: ${flags.fromFile}\n`);
      process.exit(2);
    }
    friction = loadFromFile(flags.fromFile);
    frictionSource = flags.fromFile;
  } else {
    const runId = flags.runId || activeRunId();
    try {
      friction = readFriction(runId).entries;
      frictionSource = `run-id ${runId}`;
    } catch (e) {
      process.stderr.write(
        `Note: no friction log for ${runId}; proceeding with empty friction set.\n` +
        `      (the model will likely return decision="nothing" without friction signal)\n`,
      );
      friction = [];
      frictionSource = `(no friction; run-id=${runId})`;
    }
  }

  // Configure gateway + chat availability check.
  configureGatewayForCli();
  if (!isAvailable('chat')) {
    process.stderr.write(
      'Error: no chat model is configured. Set ANTHROPIC_API_KEY (or OPENAI_API_KEY / GOOGLE_GENERATIVE_AI_API_KEY) and `chat_model` in `gbrain config`.\n',
    );
    process.exit(1);
  }

  const skills = listSkills(skillsDir);
  const userPrompt = buildUserPrompt({
    task: flags.task,
    friction,
    skills,
    maxFriction: flags.maxFriction,
  });

  // Call the gateway. Single turn, JSON-only output.
  let result: { text?: string };
  try {
    result = await chat({
      ...(flags.model ? { model: flags.model } : {}),
      system: DISTILL_SYSTEM,
      messages: [{ role: 'user', content: userPrompt }],
      maxTokens: 2000,
    });
  } catch (e) {
    process.stderr.write(`Error: chat call failed: ${(e as Error).message}\n`);
    process.exit(1);
  }

  const parsed = parseProposal(result.text ?? '');
  if (!parsed.ok) {
    process.stderr.write(`Error: model output did not parse as a valid proposal: ${parsed.error}\n`);
    process.stderr.write('--- raw output ---\n');
    process.stderr.write((result.text ?? '').slice(0, 4000));
    process.stderr.write('\n--- end ---\n');
    process.exit(1);
  }

  // Output.
  const isTty = process.stdout.isTTY === true;
  const wantJson = flags.json || !isTty;
  if (wantJson) {
    process.stdout.write(JSON.stringify(parsed.proposal, null, 2) + '\n');
  } else {
    process.stdout.write(renderHuman(parsed.proposal) + '\n');
    process.stderr.write(
      `\n(friction source: ${frictionSource} — ${friction.length} entries; ${skills.length} skills considered)\n`,
    );
  }
  process.exit(0);
}
