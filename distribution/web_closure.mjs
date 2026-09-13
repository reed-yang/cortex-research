// Sound Web payload JavaScript analyser for the distribution closure gate.
//
// The hand-written Python tokenizer this replaces mis-decided the ECMAScript
// regex-vs-division ambiguity, which let a payload hide a real, executed
// `import(...)` inside a token the analyser believed was inert. A complete
// parser is the only sound discriminator: the real payload legitimately carries
// import/export prose inside string, template, and regular-expression literals.
//
// Contract: read only the requested files, write nothing, open no network, and
// import only the vendored acorn passed as argv[2]. A single JSON request
// arrives on stdin; a single JSON document is written to stdout.
//
// See docs/plans/2026-07-29-web-closure-acorn-gate.md for the frozen contract.

import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { isAbsolute } from "node:path";

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

const acornPath = process.argv[2];
if (!acornPath || !isAbsolute(acornPath)) {
  fail("web closure analyser requires an absolute acorn path");
}
const acorn = await import(pathToFileURL(acornPath).href);

function readRequest() {
  let raw;
  try {
    raw = readFileSync(0, "utf8");
  } catch (error) {
    fail(`web closure analyser could not read its request: ${error.message}`);
  }
  let request;
  try {
    request = JSON.parse(raw);
  } catch (error) {
    fail(`web closure analyser received malformed JSON: ${error.message}`);
  }
  const files = request?.files;
  if (!Array.isArray(files)) {
    fail("web closure analyser received an invalid request");
  }
  for (const entry of files) {
    // Each entry carries the caller's relative path (for labelling the report)
    // and the exact source bytes to analyse. No path is ever resolved here.
    if (
      entry === null ||
      typeof entry !== "object" ||
      typeof entry.path !== "string" ||
      entry.path.length === 0 ||
      typeof entry.source !== "string"
    ) {
      fail("web closure analyser received an invalid file entry");
    }
  }
  return { files };
}

// Walk every child node of an ESTree tree. `visit` receives each node.
function walk(node, visit) {
  if (node === null || typeof node !== "object") {
    return;
  }
  if (Array.isArray(node)) {
    for (const child of node) {
      walk(child, visit);
    }
    return;
  }
  if (typeof node.type === "string") {
    visit(node);
  }
  for (const key of Object.keys(node)) {
    if (key === "type" || key === "start" || key === "end" || key === "loc" || key === "range") {
      continue;
    }
    walk(node[key], visit);
  }
}

// A no-substitution, unescaped template literal denotes a literal string.
// `cooked !== raw` means the template contained an escape sequence.
function templateLiteralText(node) {
  if (node.type !== "TemplateLiteral") {
    return null;
  }
  if (node.expressions.length !== 0 || node.quasis.length !== 1) {
    return null;
  }
  const quasi = node.quasis[0];
  if (typeof quasi.value.cooked !== "string" || quasi.value.cooked !== quasi.value.raw) {
    return null;
  }
  return quasi.value.cooked;
}

// A module specifier is literal only as an unescaped string literal or an
// unescaped no-substitution template literal. Everything else is a violation.
function specifierText(node) {
  if (node.type === "Literal" && typeof node.value === "string") {
    return node.raw.includes("\\") ? null : node.value;
  }
  return templateLiteralText(node);
}

// The cooked value of any literal string, including escaped forms and a
// concatenation of literals. Used for release-marker scanning, which is a
// NEGATIVE assertion and must therefore never silently drop a value: a dropped
// `"cortex-r0-build-EVI\x4C"` would be an unseen rogue marker. This is
// deliberately more permissive than `specifierText`, where an escape still
// fails closed.
function literalStringValue(node) {
  if (node === null || typeof node !== "object") {
    return null;
  }
  if (node.type === "Literal" && typeof node.value === "string") {
    return node.value;
  }
  if (node.type === "TemplateLiteral") {
    // Fold a template whose every substitution is itself a literal string: the
    // `+` case below and this one are the same construct, so treating them
    // differently would leave an asymmetric hole in marker scanning.
    let text = "";
    for (let index = 0; index < node.quasis.length; index += 1) {
      const cooked = node.quasis[index].value.cooked;
      if (typeof cooked !== "string") {
        return null;
      }
      text += cooked;
      if (index < node.expressions.length) {
        const substituted = literalStringValue(node.expressions[index]);
        if (substituted === null) {
          return null;
        }
        text += substituted;
      }
    }
    return text;
  }
  if (node.type === "BinaryExpression" && node.operator === "+") {
    const left = literalStringValue(node.left);
    const right = literalStringValue(node.right);
    return left === null || right === null ? null : left + right;
  }
  return null;
}

function stringLiterals(ast) {
  const literals = [];
  walk(ast, (node) => {
    const value = literalStringValue(node);
    if (value !== null) {
      literals.push(value);
    }
  });
  return literals;
}

function adapterDeclarations(ast) {
  // A declarator is top level only when it sits directly in Program.body,
  // optionally wrapped in a single `export` statement.
  const topLevel = new Set();
  for (const statement of ast.body) {
    const target =
      statement.type === "ExportNamedDeclaration" && statement.declaration
        ? statement.declaration
        : statement;
    if (target.type === "VariableDeclaration") {
      for (const declarator of target.declarations) {
        topLevel.add(declarator);
      }
    }
  }
  const kinds = new Map();
  walk(ast, (node) => {
    if (node.type === "VariableDeclaration") {
      for (const declarator of node.declarations) {
        kinds.set(declarator, node.kind);
      }
    }
  });
  const declarations = [];
  walk(ast, (node) => {
    if (node.type === "VariableDeclarator" && node.id?.type === "Identifier" && node.id.name === "ADAPTER_VERSION") {
      const initial = node.init;
      declarations.push({
        kind: "declarator",
        declaration: kinds.get(node) ?? "unknown",
        value:
          initial && initial.type === "Literal" && typeof initial.value === "number"
            ? initial.value
            : `NONLITERAL(${initial ? initial.type : "absent"})`,
        topLevel: topLevel.has(node),
      });
    }
    if (
      node.type === "AssignmentExpression" &&
      node.left?.type === "Identifier" &&
      node.left.name === "ADAPTER_VERSION"
    ) {
      const right = node.right;
      declarations.push({
        kind: "assignment",
        declaration: "assignment",
        value:
          right && right.type === "Literal" && typeof right.value === "number"
            ? right.value
            : `NONLITERAL(${right ? right.type : "absent"})`,
        topLevel: false,
      });
    }
  });
  return declarations;
}

function analyse(source) {
  const ast = acorn.parse(source, { ecmaVersion: "latest", sourceType: "module" });
  const references = [];
  const nonLiteralReferences = [];
  let metaProperties = 0;
  walk(ast, (node) => {
    if (node.type === "MetaProperty") {
      metaProperties += 1;
      return;
    }
    let source_ = null;
    let dynamic = false;
    if (node.type === "ImportDeclaration") {
      source_ = node.source;
    } else if (
      (node.type === "ExportNamedDeclaration" || node.type === "ExportAllDeclaration") &&
      node.source
    ) {
      source_ = node.source;
    } else if (node.type === "ImportExpression") {
      source_ = node.source;
      dynamic = true;
    }
    if (source_ === null) {
      return;
    }
    const specifier = specifierText(source_);
    if (specifier === null) {
      nonLiteralReferences.push(source_.type);
      return;
    }
    references.push({
      specifier,
      dynamic,
      literal: source_.type === "TemplateLiteral" ? "template" : "string",
    });
  });
  return {
    references,
    nonLiteralReferences,
    metaProperties,
    stringLiterals: stringLiterals(ast),
    adapterDeclarations: adapterDeclarations(ast),
  };
}

// The caller reads each file once and sends its exact bytes, so the analysed
// source is provably the source the caller hashed. This helper never touches
// the filesystem: it cannot be steered by a path, a symlink, or a racing write.
const { files } = readRequest();
const analysed = files.map(({ path, source }) => {
  try {
    return { path, error: null, ...analyse(source) };
  } catch (error) {
    return { path, error: `parse error: ${error.message}` };
  }
});
process.stdout.write(JSON.stringify({ files: analysed }));
