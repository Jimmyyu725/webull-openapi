# Webull OpenAPI 本地连接

使用 Webull 官方 Python SDK。App Key 与 App Secret 按要求直接保存在 `config.py`。

本项目当前只提供只读连接检查，不包含下单功能。

当前凭证已验证可连接 Webull Sandbox；生产端点拒绝了该组凭证。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python check_connection.py
```
