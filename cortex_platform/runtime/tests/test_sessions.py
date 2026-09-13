from __future__ import annotations

import asyncio

from cortex_platform.runtime.hermes import HermesAdapter
from cortex_platform.runtime.models import RuntimeCheckpoint, SessionOpenRequest

from .fakes import FakeHermesBackend


def test_session_create_load_fork_inspect_and_recover_contract() -> None:
    async def scenario() -> None:
        backend = FakeHermesBackend()
        adapter = HermesAdapter(backend_loader=lambda: backend)

        root = await adapter.open_session(SessionOpenRequest({"thread_id": "thread-1"}))
        assert root.adapter_id == "hermes"
        assert root.generation == 0
        assert root.parent_runtime_session_ref is None

        loaded = await adapter.load_session(root)
        assert loaded == root

        child = await adapter.fork_session(root, SessionOpenRequest())
        assert child.runtime_session_ref != root.runtime_session_ref
        assert child.parent_runtime_session_ref == root.runtime_session_ref
        assert child.generation == 1

        inspection = await adapter.inspect(child)
        assert inspection.exists is True
        assert inspection.metadata == {"source": "fake"}

        checkpoint = RuntimeCheckpoint(
            checkpoint_ref="checkpoint-1",
            conversation_history=({"role": "user", "content": "resume"},),
        )
        recovered = await adapter.recover(child, checkpoint)
        assert recovered.parent_runtime_session_ref == child.runtime_session_ref
        assert recovered.generation == 2
        assert backend.messages[recovered.runtime_session_ref] == [
            {"role": "user", "content": "resume"}
        ]

    asyncio.run(scenario())
