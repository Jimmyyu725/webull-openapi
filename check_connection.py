from webull_api import WebullAPI


def main() -> int:
    try:
        accounts = WebullAPI().accounts(refresh=True)
    except Exception:
        print("Webull OpenAPI connection failed: credentials or endpoint rejected.")
        return 1

    if accounts:
        print("Webull OpenAPI connection successful.")
        return 0

    print("Webull OpenAPI connection failed: no paper accounts returned.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
