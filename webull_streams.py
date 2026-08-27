from __future__ import annotations

import io
import json
import logging
import threading
import time
import uuid
from contextlib import redirect_stdout

import grpc

from config import (
    API_ENDPOINT,
    APP_KEY,
    APP_SECRET,
    EVENTS_ENDPOINT,
    MARKET_DATA_STREAM_ENDPOINT,
    REGION,
)
from webull.data.data_streaming_client import DataStreamingClient
from webull.trade.events import events_pb2_grpc
from webull.trade.trade_events_client import TradeEventsClient

from webull_api import redact_secrets


def _print_event(kind: str, payload) -> None:
    print(json.dumps({"type": kind, "data": payload}, ensure_ascii=False, default=str))


def _quiet_webull_logging() -> None:
    for name in ("webull", "webull.core", "webull.data"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.CRITICAL)
        logger.propagate = False


class QuietDataStreamingClient(DataStreamingClient):
    """Work around SDK 2.0.18 ignoring logger_enable in async mode."""

    def start_quietly(self) -> threading.Thread:
        self.stream_error = None

        def run() -> None:
            try:
                self.connect_and_loop_forever(logger_enable=False)
            except Exception as exc:
                self.stream_error = redact_secrets(exc)

        worker = threading.Thread(target=run, name="webull-quotes", daemon=True)
        worker.start()
        return worker


def stream_quotes(
    symbols: list[str],
    category: str,
    sub_types: list[str],
    duration: int,
    transport: str = "tcp",
    port: int = None,
) -> int:
    _quiet_webull_logging()
    session_id = f"codex_{uuid.uuid4().hex}"
    client = QuietDataStreamingClient(
        APP_KEY,
        APP_SECRET,
        REGION,
        session_id,
        http_host=API_ENDPOINT,
        mqtt_host=MARKET_DATA_STREAM_ENDPOINT,
        mqtt_port=port or (8883 if transport == "websockets" else 1883),
        transport=transport,
    )
    client.api_client.set_stream_logger(log_level=logging.CRITICAL, stream=io.StringIO())
    connected = threading.Event()

    def on_connect(stream_client, _api_client, _session_id):
        connected.set()
        _print_event("connected", {"session_id": _session_id})
        stream_client.subscribe(symbols, category, sub_types)

    def on_subscribe(_client, _api_client, _session_id):
        _print_event("subscribed", {"symbols": symbols, "category": category, "types": sub_types})

    def on_message(_client, topic, quotes):
        _print_event(topic, quotes)

    client.on_connect_success = on_connect
    client.on_subscribe_success = on_subscribe
    client.on_quotes_message = on_message
    worker = client.start_quietly()

    try:
        deadline = time.monotonic() + max(1, duration)
        while time.monotonic() < deadline and worker.is_alive():
            time.sleep(0.1)
    except KeyboardInterrupt:
        if connected.is_set():
            client.disconnect()
        return 130
    if connected.is_set():
        try:
            client.unsubscribe(unsubscribe_all=True)
        finally:
            client.disconnect()
        return 0
    _print_event("stream_error", client.stream_error or "MQTT connection ended before subscription")
    return 1


class QuietTradeEventsClient(TradeEventsClient):
    def _build_request(self, app_key, app_secret, accounts):
        with redirect_stdout(io.StringIO()):
            return super()._build_request(app_key, app_secret, accounts)

    def do_subscribe(self, accounts):
        target = self._host + ":" + str(self._port)
        if self._tls_enable:
            credentials = grpc.ssl_channel_credentials()
            with grpc.secure_channel(target, credentials) as channel:
                self._stream_processing(events_pb2_grpc.EventServiceStub(channel), accounts)
        else:
            with grpc.insecure_channel(target) as channel:
                self._stream_processing(events_pb2_grpc.EventServiceStub(channel), accounts)

    def subscribe_for(self, accounts: list[str], duration: int) -> None:
        target = self._host + ":" + str(self._port)
        if self._tls_enable:
            channel = grpc.secure_channel(target, grpc.ssl_channel_credentials())
        else:
            channel = grpc.insecure_channel(target)
        request, metadata = self._build_request(self._app_key, self._app_secret, accounts)
        responses = events_pb2_grpc.EventServiceStub(channel).Subscribe(request, metadata=metadata)
        errors = []

        def consume() -> None:
            try:
                for response in responses:
                    self._easy_handler(response)
            except grpc.RpcError as exc:
                if exc.code() != grpc.StatusCode.CANCELLED:
                    errors.append(exc)

        worker = threading.Thread(target=consume, name="webull-trade-events", daemon=True)
        worker.start()
        worker.join(max(1, duration))
        responses.cancel()
        worker.join(2)
        channel.close()
        if errors:
            raise errors[0]


def stream_trade_events(account_ids: list[str], duration: int = None) -> int:
    _quiet_webull_logging()
    client = QuietTradeEventsClient(
        APP_KEY,
        APP_SECRET,
        REGION,
        host=EVENTS_ENDPOINT,
    )
    client.on_connect = lambda _client, _payload, _response: _print_event(
        "connected", {"accounts": len(account_ids)}
    )
    client.on_events_message = lambda event_type, subscribe_type, payload, _response: _print_event(
        "trade_event",
        {"event_type": event_type, "subscribe_type": subscribe_type, "payload": payload},
    )
    if duration:
        try:
            client.subscribe_for(account_ids, duration)
            return 0
        except Exception as exc:
            _print_event("stream_error", redact_secrets(exc))
            return 1
    try:
        client.do_subscribe(account_ids)
    except KeyboardInterrupt:
        return 130
    return 0
