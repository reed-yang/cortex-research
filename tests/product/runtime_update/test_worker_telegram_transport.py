"""The worker half of `cortex.telegram.transport/1` (P5.3, D-P5-1 ruling (a)).

The socket is stdlib `urllib` inside `cortex_worker`, so these tests drive the
real module against a loopback stand-in for the Bot API. Nothing here can
reach `api.telegram.org`: the base URL is injected through the worker
environment key the product sets, the module refuses plain HTTP off loopback,
and the credential is obviously fake.
"""

from __future__ import annotations

import json

import pytest

from cortex_platform.product.runtime_update import worker_protocol
from cortex_platform.product.runtime_update.worker_payload.cortex_worker import (
    telegram as worker_telegram,
)

from .fake_bot_api import FAKE_TOKEN, FakeBotAPI


@pytest.fixture
def bot_api(monkeypatch: pytest.MonkeyPatch):
    api = FakeBotAPI()
    base_url = api.start()
    monkeypatch.setenv(worker_telegram.BASE_URL_ENV, base_url)
    monkeypatch.setenv(worker_telegram.TOKEN_ENV, FAKE_TOKEN)
    try:
        yield api
    finally:
        api.stop()


def _send_params(**overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "schema_version": 1,
        "operation_id": "telegram-delivery-1:chunk:0",
        "chat_id": 4242,
        "topic_id": None,
        "text": "hello",
        "parse_mode": "MarkdownV2",
        "buttons": [],
    }
    params.update(overrides)
    return params


class TestCapabilities:
    def test_the_worker_answers_the_frozen_protocol_string(self) -> None:
        capability = worker_telegram.capabilities()
        assert capability["protocol"] == "cortex.telegram.transport/1"
        assert set(capability) == {
            "protocol",
            "send_message",
            "topics",
            "inline_buttons",
            "markdown_v2",
            "provider_idempotency",
            "outcome_query",
        }

    def test_the_worker_does_not_claim_what_the_bot_api_cannot_do(self) -> None:
        """`getMessage` does not exist, and the product mints its own ids.

        Claiming `outcome_query` would let the product turn an
        `outcome_unknown` into a delivered message it cannot prove.
        """

        capability = worker_telegram.capabilities()
        assert capability["outcome_query"] is False
        assert capability["provider_idempotency"] is False

    def test_the_product_parses_the_worker_answer(self) -> None:
        """Both halves of the boundary agree, without importing each other."""

        from cortex_platform.product.transports.hermes import _parse_capability

        parsed = _parse_capability(worker_telegram.capabilities())
        assert parsed.protocol == "cortex.telegram.transport/1"


class TestSend:
    def test_a_send_reaches_the_bot_api_and_reports_the_message_ref(
        self, bot_api: FakeBotAPI
    ) -> None:
        result = worker_telegram.send(_send_params(), timeout=5)

        assert result["status"] == "accepted"
        assert result["provider_message_ref"] == "1001"
        call = bot_api.sent()[0]
        assert call["token"] == FAKE_TOKEN
        assert call["payload"]["chat_id"] == 4242
        assert call["payload"]["parse_mode"] == "MarkdownV2"
        assert "message_thread_id" not in call["payload"]

    def test_a_topic_send_carries_the_message_thread_id(
        self, bot_api: FakeBotAPI
    ) -> None:
        worker_telegram.send(_send_params(topic_id=7), timeout=5)
        assert bot_api.sent()[0]["payload"]["message_thread_id"] == 7

    def test_buttons_become_a_single_column_inline_keyboard(
        self, bot_api: FakeBotAPI
    ) -> None:
        worker_telegram.send(
            _send_params(
                buttons=[
                    {"label": "Approve", "callback_data": "ac1.a"},
                    {"label": "Reject", "callback_data": "ac1.r"},
                ]
            ),
            timeout=5,
        )
        markup = bot_api.sent()[0]["payload"]["reply_markup"]
        assert markup == {
            "inline_keyboard": [
                [{"text": "Approve", "callback_data": "ac1.a"}],
                [{"text": "Reject", "callback_data": "ac1.r"}],
            ]
        }

    def test_a_rate_limit_is_reported_with_its_retry_delay(
        self, bot_api: FakeBotAPI
    ) -> None:
        bot_api.queue_send_response(
            {"ok": False, "error_code": 429, "parameters": {"retry_after": 12}}
        )
        result = worker_telegram.send(_send_params(), timeout=5)
        assert result == {"status": "rate_limited", "retry_after_ms": 12_000}

    def test_a_client_error_is_a_rejection_carrying_only_the_code(
        self, bot_api: FakeBotAPI
    ) -> None:
        """The description can echo the message back, so it is never kept."""

        bot_api.queue_send_response(
            {
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: message text is empty",
            }
        )
        result = worker_telegram.send(_send_params(), timeout=5)
        assert result == {"status": "rejected", "category": "telegram_400"}

    def test_an_ok_response_without_a_message_id_is_outcome_unknown(
        self, bot_api: FakeBotAPI
    ) -> None:
        bot_api.queue_send_response({"ok": True, "result": {}})
        assert worker_telegram.send(_send_params(), timeout=5) == {
            "status": "outcome_unknown"
        }

    def test_a_send_that_never_reached_the_socket_is_retryable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing was written, so the product may claim the chunk again."""

        monkeypatch.setenv(worker_telegram.BASE_URL_ENV, "http://127.0.0.1:1")
        monkeypatch.setenv(worker_telegram.TOKEN_ENV, FAKE_TOKEN)
        assert worker_telegram.send(_send_params(), timeout=1) == {
            "status": "retryable_before_send"
        }

    def test_a_send_whose_reply_times_out_is_outcome_unknown(
        self, bot_api: FakeBotAPI
    ) -> None:
        """The request reached Telegram; only the answer did not come back.

        Reporting this `retryable_before_send` makes the product re-send a
        message the operator may already have received, and write a proof
        saying it did not. The frozen contract names timeout, malformed and
        no-response as `outcome_unknown` verbatim.
        """

        bot_api.queue_hang(2.0)

        result = worker_telegram.send(_send_params(), timeout=0.5)

        assert result == {"status": "outcome_unknown"}
        # The provider saw the request: this is exactly what makes the outcome
        # unknown rather than absent.
        assert len(bot_api.sent()) == 1

    def test_a_send_whose_connection_is_dropped_is_outcome_unknown(
        self, bot_api: FakeBotAPI
    ) -> None:
        """A reset after the request was written says nothing about delivery."""

        bot_api.queue_drop()

        result = worker_telegram.send(_send_params(), timeout=5)

        assert result == {"status": "outcome_unknown"}
        assert len(bot_api.sent()) == 1

    def test_a_send_answered_with_an_oversized_body_is_outcome_unknown(
        self, bot_api: FakeBotAPI
    ) -> None:
        """The guard refuses to hold the body; the send still happened."""

        bot_api.queue_raw_body(b'{"ok": true, "x": "' + b"a" * (1 << 20) + b'"}')

        assert worker_telegram.send(_send_params(), timeout=5) == {
            "status": "outcome_unknown"
        }

    def test_a_send_answered_with_an_unparseable_body_is_outcome_unknown(
        self, bot_api: FakeBotAPI
    ) -> None:
        """A proxy's error page is not an answer, and not a refusal either."""

        bot_api.queue_raw_body(b"<html>502 Bad Gateway</html>")

        assert worker_telegram.send(_send_params(), timeout=5) == {
            "status": "outcome_unknown"
        }

    def test_a_send_answered_with_a_json_non_object_is_outcome_unknown(
        self, bot_api: FakeBotAPI
    ) -> None:
        bot_api.queue_raw_body(b"[1, 2, 3]")

        assert worker_telegram.send(_send_params(), timeout=5) == {
            "status": "outcome_unknown"
        }

    def test_only_a_provable_pre_socket_refusal_is_retryable(self) -> None:
        """The classification is by exception CLASS, not by enumeration.

        `urllib` cannot tell the caller whether bytes were written, so the
        default has to be `outcome_unknown` and the exception that means
        `retryable_before_send` has to be raised only where nothing could have
        been sent.
        """

        assert issubclass(
            worker_telegram.TelegramRefusedBeforeSend,
            worker_telegram.TelegramTransportError,
        )

    def test_the_product_parses_every_status_the_worker_can_return(
        self, bot_api: FakeBotAPI
    ) -> None:
        from cortex_platform.product.transports import hermes

        assert hermes._parse_send_result(
            worker_telegram.send(_send_params(), timeout=5)
        ).status == "accepted"
        bot_api.queue_send_response(
            {"ok": False, "error_code": 429, "parameters": {"retry_after": 1}}
        )
        assert hermes._parse_send_result(
            worker_telegram.send(_send_params(), timeout=5)
        ).status == "rate_limited"
        bot_api.queue_send_response({"ok": False, "error_code": 400})
        assert hermes._parse_send_result(
            worker_telegram.send(_send_params(), timeout=5)
        ).status == "rejected"


class TestPoll:
    def test_a_poll_pins_allowed_updates_to_the_two_kinds_the_parser_accepts(
        self, bot_api: FakeBotAPI
    ) -> None:
        worker_telegram.poll({"offset": None, "timeout_seconds": 0}, timeout=5)
        payload = bot_api.polled()[0]["payload"]
        assert payload["allowed_updates"] == ["message", "callback_query"]
        assert payload["timeout"] == 0
        assert "offset" not in payload

    def test_a_poll_forwards_the_offset_when_one_is_known(
        self, bot_api: FakeBotAPI
    ) -> None:
        worker_telegram.poll({"offset": 91, "timeout_seconds": 1}, timeout=5)
        assert bot_api.polled()[0]["payload"]["offset"] == 91

    def test_a_poll_returns_the_updates_the_adapter_can_parse(
        self, bot_api: FakeBotAPI
    ) -> None:
        bot_api.queue_update(
            {"update_id": 5, "message": {"message_id": 1, "text": "hi"}}
        )
        # Neither kind the parser accepts: dropped rather than handed on.
        bot_api.queue_update({"update_id": 6, "edited_message": {"message_id": 2}})
        result = worker_telegram.poll({"offset": None, "timeout_seconds": 0}, timeout=5)
        assert result["status"] == "ok"
        assert [update["update_id"] for update in result["updates"]] == [5]

    def test_a_persistent_conflict_is_an_empty_poll_not_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 409 is the old poller still holding the session, not an error.

        The decision to keep holding the token belongs to the window, so the
        worker reports nothing and lets the product decide.
        """

        monkeypatch.setenv(worker_telegram.BASE_URL_ENV, "http://127.0.0.1:1")
        monkeypatch.setenv(worker_telegram.TOKEN_ENV, FAKE_TOKEN)
        assert worker_telegram.poll(
            {"offset": None, "timeout_seconds": 0}, timeout=1
        ) == {"status": "unavailable", "updates": []}

    def test_the_frame_deadline_exceeds_the_long_poll(self) -> None:
        assert worker_telegram.poll_timeout(30) > 30
        assert worker_telegram.poll_timeout(30) <= 30 + 30


class TestCredentialAndBaseUrl:
    def test_a_missing_credential_is_refused_without_a_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A relaunch after the window closed does not carry the key."""

        monkeypatch.delenv(worker_telegram.TOKEN_ENV, raising=False)
        assert worker_telegram.send(_send_params(), timeout=1) == {
            "status": "retryable_before_send"
        }
        with pytest.raises(worker_telegram.TelegramRefusedBeforeSend):
            worker_telegram._token()

    def test_the_production_default_is_the_real_https_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(worker_telegram.BASE_URL_ENV, raising=False)
        assert worker_telegram.DEFAULT_BASE_URL == "https://api.telegram.org"
        assert worker_telegram._base_url() == "https://api.telegram.org"

    @pytest.mark.parametrize(
        "value",
        [
            "http://api.telegram.org",
            "http://198.51.100.7",
            "ftp://127.0.0.1",
            "https://api.telegram.org/?x=1",
            "notaurl",
        ],
    )
    def test_a_base_url_that_is_not_https_or_loopback_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """The stand-in injection point must not double as a downgrade."""

        monkeypatch.setenv(worker_telegram.BASE_URL_ENV, value)
        with pytest.raises(worker_telegram.TelegramTransportError):
            worker_telegram._base_url()

    def test_the_credential_never_appears_in_a_transport_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(worker_telegram.BASE_URL_ENV, "http://127.0.0.1:1")
        monkeypatch.setenv(worker_telegram.TOKEN_ENV, FAKE_TOKEN)
        try:
            worker_telegram._call("sendMessage", {}, timeout=1)
        except worker_telegram.TelegramTransportError as exc:
            assert FAKE_TOKEN not in str(exc)
        else:  # pragma: no cover - the connection cannot succeed
            pytest.fail("the call should have failed")


class TestClosedProtocol:
    @pytest.mark.parametrize(
        "method",
        ["telegram.capabilities", "telegram.send", "telegram.poll"],
    )
    def test_the_three_methods_are_in_the_closed_inbound_set(self, method: str) -> None:
        assert method in worker_protocol._METHOD_FIELDS

    def test_a_send_frame_must_carry_the_frozen_request_shape(self) -> None:
        for params in (
            {},
            _send_params(text=""),
            _send_params(chat_id="4242"),
            _send_params(parse_mode="HTML"),
            _send_params(schema_version=2),
            _send_params(topic_id=0),
            _send_params(buttons=[{"label": "x"}]),
            _send_params(buttons=[{"label": "", "callback_data": "a"}]),
        ):
            with pytest.raises(worker_protocol.ProtocolViolation):
                worker_protocol.parse_request(
                    _frame("telegram.send", params), token="t" * 32
                )

    def test_a_poll_frame_is_bounded(self) -> None:
        for params in (
            {},
            {"offset": -1, "timeout_seconds": 0},
            {"offset": None, "timeout_seconds": 31},
            {"offset": None, "timeout_seconds": -1},
            {"offset": None, "timeout_seconds": "30"},
        ):
            with pytest.raises(worker_protocol.ProtocolViolation):
                worker_protocol.parse_request(
                    _frame("telegram.poll", params), token="t" * 32
                )
        worker_protocol.parse_request(
            _frame("telegram.poll", {"offset": None, "timeout_seconds": 30}),
            token="t" * 32,
        )

    def test_the_long_poll_bound_stays_far_below_the_fork_retry_ladder(self) -> None:
        """The fork's conflict ladder is 200 s; a poll must not approach it."""

        assert worker_protocol.TELEGRAM_MAX_POLL_SECONDS == 30
        assert worker_telegram.poll_timeout(
            worker_protocol.TELEGRAM_MAX_POLL_SECONDS
        ) < 200


def _frame(method: str, params: dict) -> bytes:
    return json.dumps(
        {
            "protocol": worker_protocol.PROTOCOL_V2,
            "request_id": "r1",
            "token": "t" * 32,
            "method": method,
            "params": params,
        }
    ).encode()


def test_a_control_chunk_operation_id_is_accepted_on_the_wire() -> None:
    """Found by the acceptance run, not by any unit test that preceded it.

    `_validate_params` applied the ledger's operation-id pattern to every
    method carrying that field, and Control mints chunk operation ids of the
    shape `<delivery>:chunk:<index>` -- colons the ledger pattern does not
    admit. Every real send would have been refused as a protocol violation on
    the first frame.
    """

    request = worker_protocol.parse_request(
        _frame("telegram.send", _send_params(operation_id="telegram-delivery-m10:chunk:0")),
        token="t" * 32,
    )
    assert request.params["operation_id"] == "telegram-delivery-m10:chunk:0"

    for bad in ("", ":leading", "a" * 201, "has space"):
        with pytest.raises(worker_protocol.ProtocolViolation):
            worker_protocol.parse_request(
                _frame("telegram.send", _send_params(operation_id=bad)),
                token="t" * 32,
            )

    # The ledger's own methods keep the stricter pattern.
    with pytest.raises(worker_protocol.ProtocolViolation):
        worker_protocol.parse_request(
            _frame("turn.cancel", {"operation_id": "a:b"}), token="t" * 32
        )
