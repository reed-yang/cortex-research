"""Cortex product transport adapters."""

from .hermes import HermesTelegramClient
from .models import (
    AdapterResult,
    DeliveryResult,
    OutboundMessage,
    TelegramButton,
    TelegramDestination,
    TelegramUpdate,
    TransportProblem,
)
from .ports import (
    ControlTransportChunkPort,
    ControlTransportStatePort,
    InMemoryOpaqueTargetPort,
    InMemoryTransportReceiptPort,
    SyntheticTelegramClient,
    TelegramPermanentFailure,
    TelegramRateLimited,
    TelegramTemporaryFailure,
)
from .security import OpaqueTokenService, chunk_markdown_v2, sanitize_text
from .telegram import (
    FixedWindowRateLimiter,
    TelegramAdapter,
    TelegramAdapterConfig,
)

__all__ = [
    "AdapterResult",
    "ControlTransportChunkPort",
    "ControlTransportStatePort",
    "DeliveryResult",
    "FixedWindowRateLimiter",
    "HermesTelegramClient",
    "InMemoryOpaqueTargetPort",
    "InMemoryTransportReceiptPort",
    "OpaqueTokenService",
    "OutboundMessage",
    "SyntheticTelegramClient",
    "TelegramAdapter",
    "TelegramAdapterConfig",
    "TelegramButton",
    "TelegramDestination",
    "TelegramPermanentFailure",
    "TelegramRateLimited",
    "TelegramTemporaryFailure",
    "TelegramUpdate",
    "TransportProblem",
    "chunk_markdown_v2",
    "sanitize_text",
]
