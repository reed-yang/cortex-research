// ⟦ADJ-D / ADJ-G-6 / ADJ-H-1⟧ The one Access identity length bound, owned
// here and imported by both web surfaces so the number cannot drift between
// them: `server/node-adapter.mjs` refuses a longer identity before it ever
// mints the header, and `app/api/cortex/access-security.ts` refuses the same
// length at the app boundary that reads it.
//
// 193, not 254. The daemon records a verified identity as the actor
// `access:<identity>` against a 200-character `actor_id` column, so its own
// bound is 200 - len("access:") = 193 (`_ACCESS_IDENTITY_MAXIMUM` in
// `cortex_platform/product/api/app.py`). A longer identity would authenticate
// here, open the door, and then fail every write with 400.
//
// This module is a release-payload file (`scripts/release-payload.mjs`)
// because the adapter imports it at runtime, and a release source file
// (`scripts/release-supply.mjs`) because the app imports it at build time.
export const ACCESS_IDENTITY_MAX_LENGTH = 193;
