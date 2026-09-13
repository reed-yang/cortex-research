// ⟦P7⟧ The adapter's Cloudflare Access re-verification, exercised directly:
// an RS256 assertion signed by a local key, checked against a JWKS the test
// serves itself. No network, no dependency beyond Node's `crypto`.
import assert from "node:assert/strict";
import { generateKeyPairSync } from "node:crypto";
import test from "node:test";

import {
  AUDIENCE,
  EMAIL,
  ISSUER,
  accessKey,
  base64url,
  jwksFetch,
  loadAdapterModule,
  signAssertion,
} from "./access-identity-helpers.mjs";

async function verifier(t, { keys, now }) {
  const { createAccessVerifier } = await loadAdapterModule(t);
  const fetcher = jwksFetch(keys);
  const clock = { value: now ?? Date.now() };
  const instance = createAccessVerifier({
    audience: AUDIENCE,
    fetchImplementation: fetcher.fetchImplementation,
    issuer: ISSUER,
    now: () => clock.value,
  });
  return { clock, fetcher, instance };
}

test("a well-formed assertion signed by a published key verifies to its email", async (t) => {
  const key = accessKey("kid-1");
  const { instance, fetcher } = await verifier(t, { keys: [[key]] });
  const verified = await instance.verify(signAssertion(key, {}));
  assert.deepEqual(verified.ok, true);
  assert.equal(verified.identity.email, EMAIL);
  assert.equal(fetcher.calls(), 1);
  // A second assertion with the same kid is served from the cache.
  assert.equal((await instance.verify(signAssertion(key, {}))).ok, true);
  assert.equal(fetcher.calls(), 1);
  // A string audience is accepted as well as the array form Access sends.
  assert.equal((await instance.verify(signAssertion(key, { aud: AUDIENCE }))).ok, true);
});

test("every claim the adapter relies on is checked", async (t) => {
  const key = accessKey("kid-1");
  const { instance, clock } = await verifier(t, { keys: [[key]] });
  const now = clock.value;
  const cases = {
    expired: signAssertion(key, { exp: Math.floor(now / 1000) - 120 }),
    future_iat: signAssertion(key, { iat: Math.floor(now / 1000) + 600 }),
    future_nbf: signAssertion(key, { nbf: Math.floor(now / 1000) + 600 }),
    wrong_audience: signAssertion(key, { aud: ["cd".repeat(32)] }),
    empty_audience: signAssertion(key, { aud: [] }),
    wrong_issuer: signAssertion(key, { iss: "https://other-team.cloudflareaccess.com" }),
    missing_email: signAssertion(key, { email: undefined }),
    malformed_email: signAssertion(key, { email: "not an email" }),
    no_exp: signAssertion(key, { exp: undefined }),
    no_iat: signAssertion(key, { iat: undefined }),
  };
  for (const [name, assertion] of Object.entries(cases)) {
    const verified = await instance.verify(assertion);
    assert.deepEqual(verified, { category: "access_identity_invalid", ok: false }, name);
  }
  // Within the clock-skew allowance an assertion still verifies.
  assert.equal((await instance.verify(signAssertion(key, { exp: Math.floor(now / 1000) - 30 }))).ok, true);
});

test("the signature and the header are not negotiable", async (t) => {
  const key = accessKey("kid-1");
  const other = accessKey("kid-1");
  const { instance } = await verifier(t, { keys: [[key]] });
  const invalid = { category: "access_identity_invalid", ok: false };
  assert.deepEqual(await instance.verify(signAssertion(other, {})), invalid, "wrong key, same kid");
  assert.deepEqual(await instance.verify(signAssertion(key, {}, { header: { alg: "none" }, signature: "" })), invalid, "alg none");
  assert.deepEqual(await instance.verify(signAssertion(key, {}, { header: { alg: "HS256" } })), invalid, "alg HS256");
  assert.deepEqual(await instance.verify(signAssertion(key, {}, { header: { typ: "at+jwt" } })), invalid, "typ");
  assert.deepEqual(await instance.verify(signAssertion(key, {}, { header: { kid: undefined } })), invalid, "no kid");
  const good = signAssertion(key, {});
  const [header, payload, signature] = good.split(".");
  const tampered = `${header}.${base64url({ ...JSON.parse(Buffer.from(payload, "base64url")), email: "attacker@example.test" })}.${signature}`;
  assert.deepEqual(await instance.verify(tampered), invalid, "tampered payload");
  assert.deepEqual(await instance.verify(`${header}.${payload}.${signature.slice(0, -2)}xx`), invalid, "tampered signature");
  for (const garbage of ["", "a", "a.b", "a.b.c.d", "not.base64url!.x", `${header}.${payload}.${signature}=`, "x".repeat(9000)]) {
    assert.deepEqual(await instance.verify(garbage), invalid, JSON.stringify(garbage.slice(0, 16)));
  }
});

test("an unknown kid refreshes the JWKS once, and a rotated key then verifies", async (t) => {
  const first = accessKey("kid-1");
  const second = accessKey("kid-2");
  const { instance, fetcher, clock } = await verifier(t, { keys: [[first], [first, second]] });
  assert.equal((await instance.verify(signAssertion(first, {}))).ok, true);
  assert.equal(fetcher.calls(), 1);
  // The refresh is rate-limited: an unknown kid right after a fetch costs no
  // round trip, so an attacker cannot make the adapter flood the issuer --
  // and the answer is "unverifiable" (retryable), not a verdict on the token,
  // because nobody asked the issuer about that kid.
  assert.deepEqual(await instance.verify(signAssertion(second, {})), { category: "access_identity_unverifiable", ok: false });
  assert.equal(fetcher.calls(), 1);
  clock.value += 31_000;
  assert.equal((await instance.verify(signAssertion(second, {}, { now: clock.value }))).ok, true);
  assert.equal(fetcher.calls(), 2);
  // The cache expires after an hour and is fetched again.
  clock.value += 3_600_000;
  assert.equal((await instance.verify(signAssertion(second, {}, { now: clock.value }))).ok, true);
  assert.equal(fetcher.calls(), 3);
});

test("an issuer that cannot be consulted is unverifiable until a key is cached", async (t) => {
  const key = accessKey("kid-1");
  const { instance, clock } = await verifier(t, { keys: ["unavailable", "server_error", [key]] });
  assert.deepEqual(await instance.verify(signAssertion(key, {})), { category: "access_identity_unverifiable", ok: false });
  clock.value += 31_000;
  assert.deepEqual(await instance.verify(signAssertion(key, {}, { now: clock.value })), { category: "access_identity_unverifiable", ok: false });
  clock.value += 31_000;
  assert.equal((await instance.verify(signAssertion(key, {}, { now: clock.value }))).ok, true);
  // Inside the rate-limit window after a failed refresh the answer is still
  // unverifiable -- never "invalid", never a key.
  clock.value += 10_000;
  const { instance: cold, clock: coldClock, fetcher: coldFetcher } = await verifier(t, { keys: ["unavailable"] });
  assert.deepEqual(await cold.verify(signAssertion(key, {}, { now: coldClock.value })), { category: "access_identity_unverifiable", ok: false });
  coldClock.value += 10_000;
  assert.deepEqual(await cold.verify(signAssertion(key, {}, { now: coldClock.value })), { category: "access_identity_unverifiable", ok: false });
  assert.ok(coldFetcher.calls() >= 1);
});

test("an unknown kid is invalid only after a completed fetch did not publish it", async (t) => {
  const key = accessKey("kid-1");
  const other = accessKey("kid-2");
  const { instance, clock, fetcher } = await verifier(t, { keys: [[key]] });
  assert.equal((await instance.verify(signAssertion(key, {}))).ok, true);
  // Inside the suppression window: 503, not 401.
  assert.deepEqual(await instance.verify(signAssertion(other, {})), { category: "access_identity_unverifiable", ok: false });
  assert.equal(fetcher.calls(), 1);
  // Outside it a completed fetch that still does not publish the kid is a verdict.
  clock.value += 31_000;
  assert.deepEqual(await instance.verify(signAssertion(other, {}, { now: clock.value })), { category: "access_identity_invalid", ok: false });
  assert.equal(fetcher.calls(), 2);
});

test("a warm cache survives an issuer outage only inside the staleness ceiling", async (t) => {
  const key = accessKey("kid-1");
  const { instance, clock, fetcher } = await verifier(t, { keys: [[key], "unavailable"] });
  assert.equal((await instance.verify(signAssertion(key, {}))).ok, true);
  assert.equal(fetcher.calls(), 1);
  // Past the TTL the issuer is consulted on every call; while it is down the
  // cached key still answers, because this is the deliberate grace window.
  clock.value += 3_600_000 + 1_000;
  assert.equal((await instance.verify(signAssertion(key, {}, { now: clock.value }))).ok, true);
  assert.equal(fetcher.calls(), 2, "a stale cache is never served without a consultation");
  clock.value += 10_000;
  assert.equal((await instance.verify(signAssertion(key, {}, { now: clock.value }))).ok, true);
  assert.equal(fetcher.calls(), 3, "inside the rate-limit window a STALE cache is still re-consulted");
  // Past the ceiling the door is closed whatever is cached: a retired key is
  // honoured for at most 2 h, and revocation never depends on the cache.
  clock.value += 3_600_000;
  assert.deepEqual(
    await instance.verify(signAssertion(key, {}, { now: clock.value })),
    { category: "access_identity_unverifiable", ok: false },
  );
  assert.equal(fetcher.calls(), 4);
  // And an unknown kid past the ceiling is unverifiable too, not invalid.
  assert.deepEqual(
    await instance.verify(signAssertion(accessKey("kid-9"), {}, { now: clock.value })),
    { category: "access_identity_unverifiable", ok: false },
  );
});

test("a JWKS that is not RSA-2048 signing material contributes no key", async (t) => {
  const key = accessKey("kid-1");
  const weak = generateKeyPairSync("rsa", { modulusLength: 1024 });
  const weakJwk = weak.publicKey.export({ format: "jwk" });
  const { createAccessVerifier } = await loadAdapterModule(t);
  const clock = { value: Date.now() };
  const instance = createAccessVerifier({
    audience: AUDIENCE,
    fetchImplementation: async () => Response.json({
      keys: [
        { ...weakJwk, kid: "kid-weak", kty: "RSA" },
        { ...key.jwk, kty: "EC" },
        { ...key.jwk, kid: "kid-hs", alg: "HS256" },
        { ...key.jwk, kid: "kid-enc", use: "enc" },
        "not-a-key",
      ],
    }),
    issuer: ISSUER,
    now: () => clock.value,
  });
  const weakKey = { kid: "kid-weak", privateKey: weak.privateKey };
  // Each verdict follows a COMPLETED fetch (the clock steps past the refresh
  // rate limit between calls), so "invalid" is the right answer each time.
  for (const [name, signer] of [["weak", weakKey], ["ec", key], ["hs", { ...key, kid: "kid-hs" }]]) {
    assert.deepEqual(
      await instance.verify(signAssertion(signer, {}, { now: clock.value })),
      { category: "access_identity_invalid", ok: false },
      name,
    );
    clock.value += 31_000;
  }
});

test("the verifier refuses an issuer or audience outside the contract", async (t) => {
  const { createAccessVerifier } = await loadAdapterModule(t);
  for (const [issuer, audience] of [
    ["https://evil.example/cloudflareaccess.com", AUDIENCE],
    ["http://cortex.example.test", AUDIENCE],
    ["https://example-team.cloudflareaccess.com/", AUDIENCE],
    ["https://example-team.cloudflareaccess.com", "AB".repeat(32)],
    ["https://example-team.cloudflareaccess.com", "ab".repeat(31)],
  ]) {
    assert.throws(() => createAccessVerifier({ audience, issuer }), /invalid_public_access/);
  }
  assert.ok(createAccessVerifier({ audience: AUDIENCE, issuer: "https://example-team.cloudflareaccess.com" }));
});

// ⟦ADJ-H-1⟧ The Access identity length bound is one number in one module that
// both web surfaces import. Assert the number itself -- so widening the
// daemon's 200-character `actor_id` column without widening this splits the
// three bounds loudly rather than silently -- and assert the edge through the
// adapter's real verifier, so the constant is enforced and not merely declared.
test("the shared Access identity bound is 193 and the adapter enforces it", async (t) => {
  const { ACCESS_IDENTITY_MAX_LENGTH } = await import("../server/access-identity-bound.mjs");
  // 200 - len("access:"); the daemon's twin is `_ACCESS_IDENTITY_MAXIMUM` in
  // `cortex_platform/product/api/app.py`, which asserts the same 193.
  assert.equal(ACCESS_IDENTITY_MAX_LENGTH, 193);

  // A shape the adapter's own pattern accepts, so length is the only thing
  // under test: a 60-character local part and a 132-character domain.
  const domain = `${"a".repeat(63)}.${"a".repeat(63)}.test`;
  const atBound = `${"o".repeat(ACCESS_IDENTITY_MAX_LENGTH - domain.length - 1)}@${domain}`;
  const overBound = `o${atBound}`;
  assert.equal(atBound.length, ACCESS_IDENTITY_MAX_LENGTH);
  assert.equal(overBound.length, ACCESS_IDENTITY_MAX_LENGTH + 1);

  const key = accessKey("kid-1");
  const { instance } = await verifier(t, { keys: [[key]] });
  const accepted = await instance.verify(signAssertion(key, { email: atBound }));
  assert.deepEqual(accepted.ok, true, "193 characters is admitted");
  assert.equal(accepted.identity.email, atBound);
  assert.deepEqual(
    await instance.verify(signAssertion(key, { email: overBound })),
    { category: "access_identity_invalid", ok: false },
    "194 characters is refused",
  );
});
