/**
 * Shared MCP tool-call dispatch — single source of truth for stdio + HTTP transports.
 *
 * Both transports validate the same params, build the same OperationContext shape,
 * and serialize errors identically. Drift between transports caused PR #483's reversed-args
 * + missing-context bugs; this module exists to prevent that recurring.
 */

import { appendFileSync, mkdirSync } from 'node:fs';
import { join as joinPath } from 'node:path';
import type { BrainEngine } from '../core/engine.ts';
import { operations, OperationError } from '../core/operations.ts';
import type { Operation, OperationContext } from '../core/operations.ts';
import { loadConfig } from '../core/config.ts';
import { maybeRenderUi, logGenuiDispatchEntry } from './ui-middleware.ts';

export interface ToolResult {
  content: { type: 'text'; text: string }[];
  isError?: boolean;
}

export interface DispatchOpts {
  /** Defaults to true (remote/untrusted). Local CLI callers (`gbrain call`) pass false. */
  remote?: boolean;
  /** Override the default stderr logger (e.g. CLI uses console.* directly). */
  logger?: OperationContext['logger'];
  /**
   * v0.28: per-token allow-list for the takes.holder field. Threaded by
   * the HTTP/stdio transport from `access_tokens.permissions.takes_holders`.
   * When set, takes_list / takes_search / query (when it returns takes)
   * MUST filter `WHERE holder = ANY($takesHoldersAllowList)`. Local CLI
   * callers leave this unset (no filter — they own the brain).
   */
  takesHoldersAllowList?: string[];
}

/**
 * Build a privacy-safe summary of MCP request params for logging + the admin
 * SSE feed.
 *
 * The previous default of `JSON.stringify(params)` wrote raw payloads —
 * page bodies, search queries, file paths — into `mcp_request_log` and
 * broadcast them to every connected admin browser. For a personal-knowledge
 * brain those payloads include private notes about real people / deals /
 * companies, retained indefinitely.
 *
 * The redactor returns the SHAPE of the request (what op was called, which
 * declared params were passed, approximate size) without any of the values.
 *
 * Hardening note (codex C8): a naive "dump all submitted keys" summary still
 * leaks via attacker-controlled key names — a caller can submit
 * `put_page {"wiki/people/sensitive_name": "..."}` and the key becomes a
 * persistent log entry. To prevent this, we intersect submitted keys
 * against the operation's declared `params` allow-list (the same definition
 * `validateParams` reads). Anything outside the allow-list is counted but
 * not named.
 *
 * Operators who want full payloads for debugging set `--log-full-params` on
 * `gbrain serve --http`; that path bypasses this helper and writes the raw
 * JSON, with a loud startup warning.
 */
export interface ParamSummary {
  redacted: true;
  kind: 'array' | 'object' | string;
  declared_keys?: string[];
  unknown_key_count?: number;
  length?: number;
  approx_bytes?: number;
}

/**
 * Round a byte count UP to the nearest 1KB so the redacted summary keeps a
 * coarse size signal without enabling a size-based side channel.
 *
 * Why bucketing matters: the previous shape published `approx_bytes` as the
 * exact JSON.stringify(params).length. An attacker who can submit
 * `put_page` with a known prefix and observe the resulting log entry
 * could binary-search the byte length of secret content (the body the
 * legitimate user just wrote) via repeated probes. Bucketing to 1KB
 * resolution destroys that channel while preserving the operator-useful
 * "roughly how large was the request" signal.
 */
function bucketBytes(n: number | undefined): number | undefined {
  if (n === undefined || !Number.isFinite(n)) return undefined;
  if (n <= 0) return 0;
  const KB = 1024;
  return Math.ceil(n / KB) * KB;
}

export function summarizeMcpParams(opName: string, params: unknown): ParamSummary | null {
  if (params == null) return null;

  let approxBytes: number | undefined;
  try { approxBytes = bucketBytes(JSON.stringify(params).length); } catch { approxBytes = undefined; }

  if (Array.isArray(params)) {
    return {
      redacted: true,
      kind: 'array',
      length: params.length,
      ...(approxBytes !== undefined ? { approx_bytes: approxBytes } : {}),
    };
  }

  if (typeof params === 'object') {
    const submittedKeys = Object.keys(params as Record<string, unknown>);
    const op = operations.find(o => o.name === opName);
    const allowList = op ? new Set(Object.keys(op.params)) : new Set<string>();
    const declared: string[] = [];
    let unknown = 0;
    for (const k of submittedKeys) {
      if (allowList.has(k)) declared.push(k);
      else unknown += 1;
    }
    declared.sort();
    return {
      redacted: true,
      kind: 'object',
      declared_keys: declared,
      unknown_key_count: unknown,
      ...(approxBytes !== undefined ? { approx_bytes: approxBytes } : {}),
    };
  }

  return {
    redacted: true,
    kind: typeof params,
    ...(approxBytes !== undefined ? { approx_bytes: approxBytes } : {}),
  };
}

/** Validate required params exist and have the expected type. Returns null on success, error message on failure. */
export function validateParams(op: Operation, params: Record<string, unknown>): string | null {
  for (const [key, def] of Object.entries(op.params)) {
    if (def.required && (params[key] === undefined || params[key] === null)) {
      return `Missing required parameter: ${key}`;
    }
    if (params[key] !== undefined && params[key] !== null) {
      const val = params[key];
      const expected = def.type;
      if (expected === 'string' && typeof val !== 'string') return `Parameter "${key}" must be a string`;
      if (expected === 'number' && typeof val !== 'number') return `Parameter "${key}" must be a number`;
      if (expected === 'boolean' && typeof val !== 'boolean') return `Parameter "${key}" must be a boolean`;
      if (expected === 'object' && (typeof val !== 'object' || Array.isArray(val))) return `Parameter "${key}" must be an object`;
      if (expected === 'array' && !Array.isArray(val)) return `Parameter "${key}" must be an array`;
    }
  }
  return null;
}

const stderrLogger: OperationContext['logger'] = {
  info: (msg: string) => process.stderr.write(`[info] ${msg}\n`),
  warn: (msg: string) => process.stderr.write(`[warn] ${msg}\n`),
  error: (msg: string) => process.stderr.write(`[error] ${msg}\n`),
};

export function buildOperationContext(
  engine: BrainEngine,
  params: Record<string, unknown>,
  opts: DispatchOpts = {},
): OperationContext {
  return {
    engine,
    config: loadConfig() || { engine: 'postgres' },
    logger: opts.logger || stderrLogger,
    dryRun: !!params.dry_run,
    remote: opts.remote ?? true,
    takesHoldersAllowList: opts.takesHoldersAllowList,
  };
}

/**
 * Activity log: appends one JSONL event per tool dispatch to the Hermes-side
 * unified activity log so the /ui/activity dashboard can show gbrain tool
 * calls alongside constellation/genui ones. Best-effort — any IO failure is
 * swallowed; the calling subsystem must never break because of telemetry.
 *
 * The path + schema match what `activity.py` in the daniel-hermes wrapper
 * reads. Both writers append to the same file; concurrent appendFileSync of
 * sub-PIPE_BUF lines is atomic on Linux, so no locking is needed.
 *
 * HERMES_HOME defaults to /data/.hermes which matches the wrapper's default.
 * If the env var is absent (e.g. local-dev outside the container), the write
 * silently no-ops via the try/catch.
 */
function writeActivityEntry(rec: Record<string, unknown>): void {
  try {
    const home = process.env.HERMES_HOME || '/data/.hermes';
    const dir = joinPath(home, 'activity');
    mkdirSync(dir, { recursive: true });
    // ISO week stamp — same scheme as the friction log so operators only
    // need to learn one naming convention.
    const d = new Date();
    // Crude ISO-week calc: find Thursday of the same week, then week-of-year.
    const tmp = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
    const dayNum = tmp.getUTCDay() || 7;
    tmp.setUTCDate(tmp.getUTCDate() + 4 - dayNum);
    const yearStart = new Date(Date.UTC(tmp.getUTCFullYear(), 0, 1));
    const week = Math.ceil((((tmp.getTime() - yearStart.getTime()) / 86400000) + 1) / 7);
    const ww = String(week).padStart(2, '0');
    const path = joinPath(dir, `activity-${tmp.getUTCFullYear()}-W${ww}.jsonl`);
    appendFileSync(path, JSON.stringify(rec) + '\n', { encoding: 'utf8' });
  } catch {
    // Swallow — telemetry never breaks the dispatch.
  }
}

/**
 * Resolve operation, validate params, build context, invoke handler, format result.
 *
 * Returns a `ToolResult` with the same shape both MCP transports need:
 * `{ content: [{ type: 'text', text }], isError?: boolean }`.
 */
export async function dispatchToolCall(
  engine: BrainEngine,
  name: string,
  params: Record<string, unknown> | undefined,
  opts: DispatchOpts = {},
): Promise<ToolResult> {
  const t0 = Date.now();
  const op = operations.find(o => o.name === name);
  if (!op) {
    writeActivityEntry({
      ts: new Date().toISOString().replace(/\.\d+Z$/, 'Z'),
      kind: 'tool_call', source: 'mcp_gbrain', name,
      outcome: 'error', error: 'unknown_tool',
      latency_ms: Date.now() - t0,
    });
    return { content: [{ type: 'text', text: `Error: Unknown tool: ${name}` }], isError: true };
  }

  const safeParams = params || {};
  const validationError = validateParams(op, safeParams);
  if (validationError) {
    writeActivityEntry({
      ts: new Date().toISOString().replace(/\.\d+Z$/, 'Z'),
      kind: 'tool_call', source: 'mcp_gbrain', name,
      outcome: 'error', error: 'invalid_params',
      latency_ms: Date.now() - t0,
    });
    return {
      content: [{ type: 'text', text: JSON.stringify({ error: 'invalid_params', message: validationError }, null, 2) }],
      isError: true,
    };
  }

  const ctx = buildOperationContext(engine, safeParams, opts);

  try {
    const result = await op.handler(ctx, safeParams);

    // GenUI hook (optional). Never let UI failure fail the MCP response.
    // Always-on entry log so Railway/Fly.io stderr capture shows env state +
    // result shape on every tool call until the rollout stabilizes.
    logGenuiDispatchEntry(op.name, result);
    let ui = null;
    try {
      ui = await maybeRenderUi({
        operation: op.name,
        params: safeParams,
        result,
        ctx,
      });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      ctx.logger?.warn?.(`GenUI failed: ${msg}`);
    }

    const payload = ui ? { result, ui } : result;
    writeActivityEntry({
      ts: new Date().toISOString().replace(/\.\d+Z$/, 'Z'),
      kind: 'tool_call', source: 'mcp_gbrain', name,
      outcome: 'ok',
      latency_ms: Date.now() - t0,
    });
    return { content: [{ type: 'text', text: JSON.stringify(payload, null, 2) }] };
  } catch (e: unknown) {
    const err = e instanceof OperationError ? e.code : (e instanceof Error ? e.message : String(e));
    writeActivityEntry({
      ts: new Date().toISOString().replace(/\.\d+Z$/, 'Z'),
      kind: 'tool_call', source: 'mcp_gbrain', name,
      outcome: 'error', error: String(err).slice(0, 200),
      latency_ms: Date.now() - t0,
    });
    if (e instanceof OperationError) {
      return { content: [{ type: 'text', text: JSON.stringify(e.toJSON(), null, 2) }], isError: true };
    }
    const msg = e instanceof Error ? e.message : String(e);
    return { content: [{ type: 'text', text: `Error: ${msg}` }], isError: true };
  }
}
