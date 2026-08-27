from __future__ import annotations

import inspect
import io
import json
import logging
import warnings
from dataclasses import dataclass
from typing import Any, Callable, Optional

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL.*")

from webull.core.client import ApiClient
from webull.data.data_client import DataClient
from webull.trade.trade_client import TradeClient

from config import API_ENDPOINT, APP_KEY, APP_SECRET, REGION


@dataclass
class ApiResult:
    ok: bool
    status: Optional[int]
    data: Any


def _json_body(response: Any) -> Any:
    try:
        return response.json()
    except Exception:
        return None


def normalize_result(value: Any) -> ApiResult:
    if hasattr(value, "status_code"):
        status = int(value.status_code)
        return ApiResult(status < 400, status, _json_body(value))
    return ApiResult(True, None, value)


def redact_secrets(value: Any) -> str:
    text = str(value)
    for secret in (APP_KEY, APP_SECRET):
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def print_result(result: ApiResult) -> int:
    payload = {"ok": result.ok, "status": result.status, "data": result.data}
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0 if result.ok else 1


class WebullAPI:
    TARGETS = (
        "trade.account",
        "trade.account_v2",
        "trade.orders",
        "trade.orders_v2",
        "trade.orders_v3",
        "trade.activity",
        "trade.instruments",
        "trade.calendar",
        "data.instruments",
        "data.market",
        "data.crypto",
        "data.futures",
        "data.options",
        "data.events",
        "data.fundamentals",
        "data.screener",
        "data.watchlist",
    )

    def __init__(self) -> None:
        self.api_client = ApiClient(APP_KEY, APP_SECRET, REGION)
        self.api_client.add_endpoint(REGION, API_ENDPOINT)

        # Mark logging as configured so the SDK does not create credential-bearing log files.
        self._log_sink = io.StringIO()
        self.api_client.set_stream_logger(log_level=logging.CRITICAL, stream=self._log_sink)

        self.trade = TradeClient(self.api_client)
        self.data = DataClient(self.api_client)
        self._accounts: Optional[list[dict[str, Any]]] = None

    def target(self, name: str) -> Any:
        targets = {
            "trade.account": self.trade.account,
            "trade.account_v2": self.trade.account_v2,
            "trade.orders": self.trade.order,
            "trade.orders_v2": self.trade.order_v2,
            "trade.orders_v3": self.trade.order_v3,
            "trade.activity": self.trade.activity,
            "trade.instruments": self.trade.trade_instrument,
            "trade.calendar": self.trade.trade_calendar,
            "data.instruments": self.data.instrument,
            "data.market": self.data.market_data,
            "data.crypto": self.data.crypto_market_data,
            "data.futures": self.data.futures_market_data,
            "data.options": self.data.option_market_data,
            "data.events": self.data.event_market_data,
            "data.fundamentals": self.data.fundamentals,
            "data.screener": self.data.screener,
            "data.watchlist": self.data.watchlist,
        }
        try:
            return targets[name]
        except KeyError as exc:
            raise ValueError(f"Unknown SDK target: {name}") from exc

    def catalog(self, target_name: Optional[str] = None) -> dict[str, dict[str, str]]:
        names = [target_name] if target_name else list(self.TARGETS)
        result: dict[str, dict[str, str]] = {}
        for name in names:
            obj = self.target(name)
            methods: dict[str, str] = {}
            for method_name, method in inspect.getmembers(obj, callable):
                if method_name.startswith("_"):
                    continue
                try:
                    signature = str(inspect.signature(method))
                except (TypeError, ValueError):
                    signature = "(...)"
                methods[method_name] = signature
            result[name] = methods
        return result

    def invoke(self, path: str, args: list[Any], kwargs: dict[str, Any]) -> ApiResult:
        try:
            target_name, method_name = path.rsplit(".", 1)
        except ValueError as exc:
            raise ValueError("Call path must look like data.market.get_snapshot") from exc

        if method_name.startswith("_"):
            raise ValueError("Private SDK methods are not callable")
        method: Callable[..., Any] = getattr(self.target(target_name), method_name, None)
        if not callable(method):
            raise ValueError(f"Unknown SDK method: {path}")
        try:
            return normalize_result(method(*args, **kwargs))
        except Exception as exc:
            return ApiResult(False, getattr(exc, "http_status", None), {
                "error": type(exc).__name__,
                "message": redact_secrets(exc),
            })

    def accounts(self, refresh: bool = False) -> list[dict[str, Any]]:
        if self._accounts is None or refresh:
            result = normalize_result(self.trade.account_v2.get_account_list())
            if not result.ok or not isinstance(result.data, list):
                raise RuntimeError("Unable to retrieve Webull accounts")
            self._accounts = result.data
        return self._accounts

    def account(self, reference: Optional[str]) -> dict[str, Any]:
        accounts = self.accounts()
        if reference is None:
            if len(accounts) == 1:
                return accounts[0]
            raise ValueError("Choose an account with --account-class or --account")

        wanted = reference.upper()
        matches = [
            account for account in accounts
            if wanted in {
                str(account.get("account_id", "")).upper(),
                str(account.get("account_number", "")).upper(),
                str(account.get("account_class", "")).upper(),
                str(account.get("account_label", "")).upper(),
            }
        ]
        if len(matches) != 1:
            raise ValueError(f"Account reference matched {len(matches)} accounts: {reference}")
        return matches[0]

    def account_id(self, reference: Optional[str]) -> str:
        return str(self.account(reference)["account_id"])


MUTATING_METHOD_PREFIXES = (
    "add_",
    "batch_place",
    "cancel_",
    "create_",
    "delete_",
    "place_",
    "remove_",
    "replace_",
    "update_",
)


def is_mutating_call(path: str) -> bool:
    method = path.rsplit(".", 1)[-1]
    return method.startswith(MUTATING_METHOD_PREFIXES)
