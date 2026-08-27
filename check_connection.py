import logging

from webull.core.client import ApiClient
from webull.trade.trade_client import TradeClient

from config import API_ENDPOINT, APP_KEY, APP_SECRET, REGION


def main() -> int:
    api_client = ApiClient(APP_KEY, APP_SECRET, REGION)
    api_client.add_endpoint(REGION, API_ENDPOINT)

    logging.disable(logging.CRITICAL)
    try:
        response = TradeClient(api_client).account_v2.get_account_list()
    except Exception:
        print("Webull OpenAPI connection failed: credentials or endpoint rejected.")
        return 1
    finally:
        logging.disable(logging.NOTSET)

    if response.status_code == 200:
        print("Webull OpenAPI connection successful.")
        return 0

    print(f"Webull OpenAPI connection failed: HTTP {response.status_code}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
