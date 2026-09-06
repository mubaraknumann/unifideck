import { describe, expect, it } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

/**
 * Every `unifideck-*-btn` class a component puts on a button must have a rule
 * behind it, and that rule must include a focus state.
 *
 * The bug that prompted this: `unifideck-download-update-btn` was applied to
 * the Update button in the Downloads tab and styled nowhere. It read to the
 * user as a dead button: with no focus rule the gamepad got no feedback at
 * all when it reached the button, so the only way to press it was the
 * trackpad. It was focusable the whole time; nothing on screen said so.
 *
 * A class name is a string on both sides, so nothing but a test connects
 * them.
 */
function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) return walk(full);
    return /\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry) ? [full] : [];
  });
}

const BUTTON_CLASS = /"(unifideck-[a-z0-9-]+-btn)"/g;

// Paths come from `import.meta.url`, not `__dirname`: this file is ESM, where
// `__dirname` exists only because Vitest shims it in. The shim is not part of
// the module format, so it disappears under any other runner. Same idiom
// vitest.config.ts already uses.
const here = (rel: string) => fileURLToPath(new URL(rel, import.meta.url));

// Read as text rather than imported: play.css.ts pulls in @decky/ui, which
// needs a React runtime this suite deliberately does not have.
const CSS = readFileSync(here("./play.css.ts"), "utf-8");

const used = new Set<string>();
for (const file of walk(here("../../"))) {
  const source = readFileSync(file, "utf-8");
  if (file.endsWith("play.css.ts")) continue;
  for (const [, name] of source.matchAll(BUTTON_CLASS)) used.add(name);
}

describe("button classes used by components", () => {
  it("finds the classes at all, so an empty pass is not a pass", () => {
    expect(used.size).toBeGreaterThan(2);
  });

  // Selectors are often grouped (".a,\n.b,\n.c {"), so a class counts as
  // styled when it appears as a selector at all, not only when it opens its
  // own block.
  const selector = (name: string) => new RegExp(`\\.${name}[\\s,:.{]`);

  it.each([...used])("%s is styled", (name) => {
    expect(CSS).toMatch(selector(name));
  });

  it.each([...used])("%s reacts to gamepad focus", (name) => {
    // Steam puts `.gpfocus` on the focused element. Without a rule that
    // mentions it, the button gives the user nothing to see and reads as
    // disabled — which is exactly how the Downloads Update button shipped.
    expect(CSS).toMatch(new RegExp(`\\.${name}\\.gpfocus`));
  });
});
