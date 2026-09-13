import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// `app/globals.css` is the only stylesheet that declares theme tokens:
// `app/fonts.css` carries font faces and `app/shadcn-tailwind.css` carries
// utility-local state (`--scroll-fade-*`, always read with a fallback and
// declared in the scope that reads it), neither of which belongs in a :root
// resolution check.
const stylesheet = readFileSync(resolve(process.cwd(), "app/globals.css"), "utf8");

function rootBlocks(css: string): string[] {
  return [...css.matchAll(/:root\s*\{([^}]*)\}/g)].map((match) => match[1]!);
}

function declarations(block: string): Map<string, string> {
  return new Map([...block.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)].map((match) => [match[1]!, match[2]!.trim()]));
}

// The `:root` inside `@media (prefers-color-scheme: dark)`. The file also
// carries `@custom-variant dark (@media (prefers-color-scheme: dark));` on one
// line, so the block form is found by brace matching rather than by a regex
// that would stop at the first `)`.
function darkRootBlock(css: string): string {
  const opener = /@media\s*\(prefers-color-scheme:\s*dark\)\s*\{/.exec(css);
  expect(opener).not.toBeNull();
  let depth = 0;
  let index = opener!.index + opener![0].length - 1;
  const start = index;
  for (; index < css.length; index += 1) {
    if (css[index] === "{") depth += 1;
    else if (css[index] === "}") {
      depth -= 1;
      if (depth === 0) break;
    }
  }
  const blocks = rootBlocks(css.slice(start, index));
  expect(blocks).toHaveLength(1);
  return blocks[0]!;
}

// Every rule in the file, with the at-rules it sits inside. Comments are
// skipped, at-statements (`@import`, `@custom-variant`) end at their semicolon,
// and a declaration block is pushed like any other scope so the nesting depth
// stays honest.
function styleRules(css: string): { selector: string; layered: boolean }[] {
  const rules: { selector: string; layered: boolean }[] = [];
  const scopes: string[] = [];
  let prelude = "";
  for (let index = 0; index < css.length; index += 1) {
    const character = css[index];
    if (character === "/" && css[index + 1] === "*") {
      const end = css.indexOf("*/", index + 2);
      index = end === -1 ? css.length : end + 1;
      continue;
    }
    if (character === "{") {
      const opened = prelude.trim();
      prelude = "";
      if (opened.startsWith("@")) scopes.push(opened);
      else {
        rules.push({ selector: opened, layered: scopes.some((scope) => scope.startsWith("@layer")) });
        scopes.push("");
      }
      continue;
    }
    if (character === "}") {
      scopes.pop();
      prelude = "";
      continue;
    }
    if (character === ";") {
      prelude = "";
      continue;
    }
    prelude += character;
  }
  return rules;
}

describe("shell stylesheet", () => {
  it("resolves every custom property it paints with", () => {
    // Only :root blocks count as declarations, so a token quietly moved
    // into a local selector stops resolving for the rules that paint with it.
    // Tokens that legitimately live in element scope go here explicitly.
    const elementScoped = new Set<string>([
      "--radix-collapsible-content-height",
      "--collapsible-panel-height",
    ]);
    const declared = new Set(rootBlocks(stylesheet).flatMap((block) => [...declarations(block).keys()]));
    expect(declared.size).toBeGreaterThan(0);
    const used = new Set([...stylesheet.matchAll(/var\((--[a-z0-9-]+)/g)].map((match) => match[1]!));
    expect([...used].filter((token) => !declared.has(token) && !elementScoped.has(token))).toEqual([]);
  });

  it("keeps every legacy palette token on the theme in both colour schemes", () => {
    // The legacy tokens are the July cockpit's palette, still painted by the
    // research components and the retained demo prototype. Each one is an alias
    // of the shadcn token that plays the same role; a literal colour here would
    // be a fixed light value on a surface that inverts, which is the whole
    // failure the bridge exists to prevent.
    const bridge = rootBlocks(stylesheet).map(declarations).find((block) => block.has("--paper"));
    expect(bridge).toBeDefined();
    const dark = new Set(declarations(darkRootBlock(stylesheet)).keys());
    const literalColour = /#[0-9a-f]{3,8}\b|\b(?:rgba?|hsla?|oklch|oklab|lab|lch|color)\s*\(/i;
    const unthemed: string[] = [];
    for (const [token, value] of bridge!) {
      const alias = /^var\((--[a-z0-9-]+)\)$/.exec(value);
      if (alias) {
        // The alias resolves per scheme only if the token it points at is
        // redefined for dark.
        if (!dark.has(alias[1]!)) unthemed.push(`${token} -> ${alias[1]} (no dark value)`);
        continue;
      }
      // A non-alias is allowed only when it carries no colour at all.
      if (literalColour.test(value)) unthemed.push(`${token}: ${value} (fixed colour)`);
    }
    expect(unthemed).toEqual([]);
  });

  it("leaves no unlayered element rule outside the prototype", () => {
    // An unlayered declaration outranks every rule in Tailwind's `utilities`
    // layer whatever its specificity, so a bare `button { color: inherit }`
    // silently beats `text-primary-foreground` on every default button in the
    // shell. The July cockpit's element rules survive only for the retained
    // `?mode=demo` prototype, and only under its root: a selector carrying no
    // class, id or attribute anchor may not name one of these elements or the
    // focus-visible state outside a layer.
    const named = /(?:^|[\s>+~])(?:a|button|input|select|summary|textarea)(?![\w-])/;
    const anchored = /[.#[]/;
    const unscoped: string[] = [];
    for (const rule of styleRules(stylesheet)) {
      if (rule.layered) continue;
      for (const selector of rule.selector.split(",").map((part) => part.trim()).filter(Boolean)) {
        if (anchored.test(selector)) continue;
        if (named.test(selector) || selector.includes(":focus-visible")) unscoped.push(selector);
      }
    }
    expect(unscoped).toEqual([]);
  });
});
