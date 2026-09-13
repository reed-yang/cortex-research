import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const control = new URL("../app/control/", import.meta.url);
const dataUrl = (text) => `data:text/javascript;base64,${Buffer.from(text).toString("base64")}`;
const contracts = ts.transpileModule(await readFile(new URL("contracts.ts", control), "utf8"), {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText;
const source = new URL("research-contracts.ts", control);
const compiled = ts.transpileModule(await readFile(source, "utf8"), {
  fileName: fileURLToPath(source),
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  transformers: { before: [(context) => {
    const visit = (node) => {
      if (ts.isImportDeclaration(node) && node.moduleSpecifier.text === "./contracts") {
        return ts.factory.updateImportDeclaration(node, node.modifiers, node.importClause, ts.factory.createStringLiteral(dataUrl(contracts)), node.attributes);
      }
      return ts.visitEachChild(node, visit, context);
    };
    return (node) => ts.visitNode(node, visit);
  }] },
}).outputText;
const { decodeResearchWorkflow } = await import(dataUrl(compiled));
let body = "";
for await (const chunk of process.stdin) body += chunk;
try {
  const value = decodeResearchWorkflow(JSON.parse(body));
  console.log(JSON.stringify({ ok: true, value }));
} catch (error) {
  console.log(JSON.stringify({ ok: false, error: error.message }));
  process.exitCode = 1;
}
