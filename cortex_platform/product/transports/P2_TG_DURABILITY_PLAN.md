# P2 Telegram Durability Slice 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move sanitized Telegram command receipts, outbound delivery leases, and opaque callback/deep-link targets into migration 8 of the Control-owned database, then wire the existing synthetic adapter to that durable boundary.

**Architecture:** Migration 8 adds transport-generic tables keyed only by opaque digests and stable Cortex transport identities. `ControlStore` exposes typed request/result objects for command replay, fenced delivery claims, and atomic opaque-target consumption; a transport-side adapter implements the existing receipt and target ports without creating another database. `OpaqueTokenService` keeps one primary signing key plus a bounded set of previous verification keys so persisted targets remain usable during explicit key rotation.

**Tech Stack:** Python 3.14, SQLite, dataclasses, pytest, ruff.

---

### Task 1: Control-owned transport durability schema and store boundary

**Files:**
- Create: `cortex_platform/product/control/transport.py`
- Modify: `cortex_platform/product/control/schema.py`
- Modify: `cortex_platform/product/control/store.py`
- Modify: `cortex_platform/product/control/__init__.py`
- Modify: `cortex_platform/tests/product/control/test_store.py`

- [x] **Step 1: Write focused failing migration and store tests**

Add one primary behavior test for each invariant: migration 8 is additive and repeatable; a sanitized command result replays after `ControlStore` reconstruction and rejects request-hash drift; an expired outbound lease is reclaimed with a higher fence while stale completion fails; an opaque target persists, enforces atomic single-consumer semantics, and remains distinguishable after expiry.

- [x] **Step 2: Run the focused tests and verify RED**

Run: `uv run --frozen pytest -q cortex_platform/tests/product/control/test_store.py -k 'migration or transport_command or transport_delivery or transport_opaque'`

Expected: FAIL because schema version 8 and the typed transport store API do not exist.

- [x] **Step 3: Implement migration 8 and typed store operations**

Create immutable transport key/receipt/claim/target dataclasses. Add transport-generic command receipt, delivery lease, and opaque-target tables without changing migrations 1-7. Add validated `ControlStore` methods that transact replay/conflict checks, lease fencing/reclaim/completion/release, and atomic target put/read/consume using only opaque identifiers and sanitized JSON.

- [x] **Step 4: Run focused Control tests and verify GREEN**

Run: `uv run --frozen pytest -q cortex_platform/tests/product/control/test_store.py -k 'migration or transport_command or transport_delivery or transport_opaque'`

Expected: PASS.

- [x] **Step 5: Run the complete Control suite**

Run: `uv run --frozen pytest -q cortex_platform/tests/product/control`

Expected: PASS.

- [x] **Step 6: Commit the schema/store slice**

Commit the migration, typed boundary, exports, tests, and this plan as `feat(control): persist transport delivery state` with the required assisted-by trailer.

### Task 2: Minimal Telegram adapter wiring and key rotation

**Files:**
- Modify: `cortex_platform/product/transports/ports.py`
- Modify: `cortex_platform/product/transports/security.py`
- Modify: `cortex_platform/product/transports/telegram.py`
- Modify: `cortex_platform/product/transports/__init__.py`
- Modify: `cortex_platform/tests/product/transports/test_telegram.py`

- [x] **Step 1: Write focused failing adapter tests**

Add one restart test covering sanitized inbound replay and one restart test covering expired outbound lease reclaim plus persisted opaque targets. The token test issues with an old key, reconstructs with a new primary plus the old verification key, verifies wrong-purpose resolution does not consume, then verifies one successful consumption and expiry behavior.

- [x] **Step 2: Run the focused tests and verify RED**

Run: `uv run --frozen pytest -q cortex_platform/tests/product/transports/test_telegram.py -k 'control_owned or key_rotation or lease_recovery'`

Expected: FAIL because the durable port and Control-owned adapter constructor do not exist.

- [x] **Step 3: Implement the durable port and minimal adapter constructor**

Implement a transport-side port over the typed `ControlStore` boundary, including explicit worker identity and lease duration. Add bounded previous-key verification to `OpaqueTokenService` and a `TelegramAdapter.control_owned(...)` constructor that wires both durable ports while leaving synthetic/in-memory injection unchanged.

- [x] **Step 4: Run focused transport tests and verify GREEN**

Run: `uv run --frozen pytest -q cortex_platform/tests/product/transports/test_telegram.py`

Expected: PASS.

- [x] **Step 5: Run product regression and static gates**

Run: `uv run --frozen pytest -q cortex_platform/tests/product/transports cortex_platform/tests/product/control`

Run: `uv run --frozen pytest -q cortex_platform/tests/product`

Run: `uv run --frozen --with ruff ruff check cortex_platform/product/control cortex_platform/product/transports cortex_platform/tests/product/control cortex_platform/tests/product/transports`

Run: `uv run --frozen python -m compileall -q cortex_platform/product/control cortex_platform/product/transports cortex_platform/tests/product/control cortex_platform/tests/product/transports`

Run: `git diff --check`

Expected: all commands exit 0.

- [x] **Step 6: Commit the adapter wiring slice**

Commit the durable port, key rotation, adapter constructor, tests, and handoff evidence as `feat(telegram): wire control-owned durability` with the required assisted-by trailer. Do not push.
