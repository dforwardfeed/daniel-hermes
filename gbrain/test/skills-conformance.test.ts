import { describe, test, expect } from "bun:test";
import { readFileSync, existsSync, readdirSync } from "fs";
import { join } from "path";

const SKILLS_DIR = join(import.meta.dir, "..", "skills");
const MANIFEST_PATH = join(SKILLS_DIR, "manifest.json");

/** Simple YAML frontmatter parser — extracts fields between --- delimiters */
function parseFrontmatter(content: string): Record<string, unknown> | null {
  const match = content.match(/^---\n([\s\S]*?)\n---/);
  if (!match) return null;
  const yaml = match[1];
  const result: Record<string, string> = {};
  for (const line of yaml.split("\n")) {
    const colonIdx = line.indexOf(":");
    if (colonIdx > 0) {
      const key = line.slice(0, colonIdx).trim();
      const value = line.slice(colonIdx + 1).trim();
      if (key && !key.startsWith(" ") && !key.startsWith("-")) {
        result[key] = value;
      }
    }
  }
  return result;
}

/** Get all skill directories (those containing SKILL.md) */
function getSkillDirs(): string[] {
  const entries = readdirSync(SKILLS_DIR, { withFileTypes: true });
  return entries
    .filter((e) => e.isDirectory())
    .filter((e) => existsSync(join(SKILLS_DIR, e.name, "SKILL.md")))
    .map((e) => e.name)
    .filter((name) => name !== "install"); // deprecated skill
}

describe("skills conformance", () => {
  const skillDirs = getSkillDirs();

  test("manifest.json exists and is valid JSON", () => {
    expect(existsSync(MANIFEST_PATH)).toBe(true);
    const manifest = JSON.parse(readFileSync(MANIFEST_PATH, "utf-8"));
    expect(manifest.skills).toBeDefined();
    expect(Array.isArray(manifest.skills)).toBe(true);
  });

  test("manifest lists every skill directory", () => {
    const manifest = JSON.parse(readFileSync(MANIFEST_PATH, "utf-8"));
    const manifestNames = manifest.skills.map((s: { name: string }) => s.name);
    for (const dir of skillDirs) {
      expect(manifestNames).toContain(dir);
    }
  });

  test("every manifest entry points to an existing SKILL.md", () => {
    const manifest = JSON.parse(readFileSync(MANIFEST_PATH, "utf-8"));
    for (const skill of manifest.skills) {
      const skillPath = join(SKILLS_DIR, skill.path);
      expect(existsSync(skillPath)).toBe(true);
    }
  });

  for (const dir of skillDirs) {
    describe(`skills/${dir}/SKILL.md`, () => {
      const content = readFileSync(join(SKILLS_DIR, dir, "SKILL.md"), "utf-8");

      test("has YAML frontmatter", () => {
        expect(content.startsWith("---\n")).toBe(true);
        const fm = parseFrontmatter(content);
        expect(fm).not.toBeNull();
      });

      test("frontmatter has required fields (name, description)", () => {
        const fm = parseFrontmatter(content);
        expect(fm).not.toBeNull();
        expect(fm!.name).toBeDefined();
        expect(fm!.description).toBeDefined();
      });

      test("has a Contract section", () => {
        expect(content).toContain("## Contract");
      });

      test("has an Anti-Patterns section", () => {
        expect(content).toContain("## Anti-Patterns");
      });

      test("has an Output Format section", () => {
        expect(content).toContain("## Output Format");
      });
    });
  }

  // Experience-distillation sections. New scaffolds emit these three; existing
  // skills will be backfilled over time. The first release runs in SOFT-WARN
  // mode — missing sections print a single summary line to stderr but do NOT
  // fail the test — so the conformance ratchet doesn't break CI overnight.
  // Set GBRAIN_CONFORMANCE_STRICT=1 to flip to hard-required (used in the
  // next release once backfill is complete).
  //
  // `## Anti-Patterns` is recognized as an equivalent of `## When NOT to Use`
  // during the ratchet window so the existing 29-skill library doesn't have
  // to flip terminology at the same time as adopting the new sections.
  describe("experience-distillation sections", () => {
    const STRICT = process.env.GBRAIN_CONFORMANCE_STRICT === "1";
    const missingByDir: Record<string, string[]> = {};

    for (const dir of skillDirs) {
      const content = readFileSync(join(SKILLS_DIR, dir, "SKILL.md"), "utf-8");
      const missing: string[] = [];
      const hasWhenNot =
        content.includes("## When NOT to Use") ||
        content.includes("## Anti-Patterns");
      if (!hasWhenNot) missing.push("When NOT to Use");
      if (!content.includes("## Common Failure Modes")) missing.push("Common Failure Modes");
      if (!content.includes("## Recovery Strategy")) missing.push("Recovery Strategy");
      if (missing.length > 0) missingByDir[dir] = missing;
    }

    if (STRICT) {
      for (const [dir, missing] of Object.entries(missingByDir)) {
        test(`skills/${dir}/SKILL.md has experience-distillation sections`, () => {
          expect({ skill: dir, missing }).toEqual({ skill: dir, missing: [] });
        });
      }
      test("(strict) all skills carry experience-distillation sections", () => {
        expect(Object.keys(missingByDir).length).toBe(0);
      });
    } else {
      test("(soft-warn) report skills missing experience-distillation sections", () => {
        const total = Object.keys(missingByDir).length;
        if (total > 0) {
          const lines = [
            `[conformance] ${total} skill(s) missing experience-distillation sections`,
            `  Set GBRAIN_CONFORMANCE_STRICT=1 to make this fail CI.`,
            ...Object.entries(missingByDir).map(
              ([dir, missing]) => `  - skills/${dir}: ${missing.join(", ")}`,
            ),
          ];
          // Single multi-line write so the message is one coherent block,
          // not 50+ scattered lines breaking up the bun test output.
          process.stderr.write(lines.join("\n") + "\n");
        }
        expect(true).toBe(true);
      });
    }
  });

  test("no duplicate skill names in frontmatter", () => {
    const names: string[] = [];
    for (const dir of skillDirs) {
      const content = readFileSync(join(SKILLS_DIR, dir, "SKILL.md"), "utf-8");
      const fm = parseFrontmatter(content);
      if (fm?.name) {
        const name = String(fm.name);
        expect(names).not.toContain(name);
        names.push(name);
      }
    }
  });
});
