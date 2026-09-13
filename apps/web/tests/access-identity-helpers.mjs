// ⟦P7⟧ Test-only material for the public door: a local RS256 key that stands
// in for a Cloudflare Access team key, an assertion signer, a JWKS fetch
// stand-in, and a way to import the source adapter without a built app.
import assert from "node:assert/strict";
import { generateKeyPairSync, sign } from "node:crypto";
import { cp, mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const ISSUER = "http://127.0.0.1:8123";
export const AUDIENCE = "ab".repeat(32);
export const EMAIL = "operator@example.test";

export function accessKey(kid) {
  const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const jwk = publicKey.export({ format: "jwk" });
  return {
    jwk: { alg: "RS256", e: jwk.e, kid, kty: "RSA", n: jwk.n, use: "sig" },
    kid,
    privateKey,
  };
}

export function base64url(value) {
  return Buffer.from(typeof value === "string" ? value : JSON.stringify(value)).toString("base64url");
}

export function signAssertion(key, claims, options = {}) {
  const header = { alg: "RS256", kid: key.kid, typ: "JWT", ...(options.header ?? {}) };
  const now = Math.floor((options.now ?? Date.now()) / 1000);
  const payload = {
    aud: [AUDIENCE],
    email: EMAIL,
    exp: now + 600,
    iat: now,
    iss: ISSUER,
    sub: "subject-1",
    type: "app",
    ...claims,
  };
  const signed = `${base64url(header)}.${base64url(payload)}`;
  const signature = options.signature ??
    sign("sha256", Buffer.from(signed, "ascii"), key.privateKey).toString("base64url");
  return `${signed}.${signature}`;
}

export function jwksFetch(keysByCall) {
  let call = 0;
  const fetchImplementation = async (url) => {
    assert.equal(url, `${ISSUER}/cdn-cgi/access/certs`);
    const keys = keysByCall[Math.min(call, keysByCall.length - 1)];
    call += 1;
    if (keys === "unavailable") throw new Error("connect_refused");
    if (keys === "server_error") return new Response("nope", { status: 500 });
    return Response.json({ keys: keys.map((key) => key.jwk) });
  };
  return { calls: () => call, fetchImplementation };
}

// The adapter imports `./index.js` (the built app) at load time, so the source
// file cannot be imported in place. A copy beside a stub app is enough to
// exercise the exported verifier.
export async function loadAdapterModule(t) {
  const parent = await mkdtemp(path.join(os.tmpdir(), "cortex-access-identity-"));
  t.after(() => rm(parent, { recursive: true, force: true }));
  await writeFile(
    path.join(parent, "index.js"),
    "export default { async fetch() { return new Response('stub'); } };\n",
  );
  for (const relative of ["server/access-identity-bound.mjs", "server/node-adapter.mjs"]) {
    await cp(path.join(webRoot, relative), path.join(parent, path.basename(relative)));
  }
  return import(pathToFileURL(path.join(parent, "node-adapter.mjs")).href);
}
