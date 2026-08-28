# Webull OpenAPI 模拟盘工具

这是一个基于 Webull 官方 Python SDK 的本地命令行工具，固定连接 Webull Sandbox / Paper Trading。凭证按项目要求直接保存在 `config.py`，不会通过命令行参数或环境变量传递。

工具覆盖账户、余额、持仓、活动、订单预览/下单/改单/撤单/查询、股票/期权/期货/加密货币/事件合约、行情、标的发现、自选列表、基本面、筛选器、行情流和交易事件流。`catalog` 与 `call` 会自动暴露当前 SDK 的全部公开方法，因此 SDK 增加接口后通常不必再新增包装代码。

## 安装与自检

```bash
cd /Users/jingtianyu/Documents/Codex/webull-openapi
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python webull_cli.py doctor
.venv/bin/python webull_cli.py capabilities
```

`doctor` 验证密钥和模拟账户；`capabilities` 对各类只读接口做安全探测。当前密钥已确认具有以下 Sandbox 账户类别：

- `INDIVIDUAL_CASH`
- `INDIVIDUAL_MARGIN`
- `FUTURES`
- `CRYPTO`
- `EVENTS_CASH`

默认订单账户映射：股票和期权使用 `INDIVIDUAL_MARGIN`，期货使用 `FUTURES`，加密货币使用 `CRYPTO`，事件合约使用 `EVENTS_CASH`。可用 `--account` 或 `--account-class` 覆盖。

## 安全边界

- `config.py` 中的 HTTP、行情流和交易事件端点全部是 Sandbox。
- 下单、改单、撤单、批量下单以及通用写接口必须显式传入 `--yes`。
- SDK 原本可能生成包含签名元数据的本地日志；本项目已禁用这些文件日志，并对错误文本中的凭证做脱敏。
- 模拟订单仍可能改变模拟账户的余额、持仓与历史记录。先用 `order preview`，确认后再用 `order place --yes`。

## 账户和订单查询

```bash
.venv/bin/python webull_cli.py accounts
.venv/bin/python webull_cli.py balance --account-class INDIVIDUAL_MARGIN
.venv/bin/python webull_cli.py positions --account-class INDIVIDUAL_MARGIN
.venv/bin/python webull_cli.py activities --account-class INDIVIDUAL_MARGIN --page-size 20
.venv/bin/python webull_cli.py orders open --account-class INDIVIDUAL_MARGIN
.venv/bin/python webull_cli.py orders history --account-class INDIVIDUAL_MARGIN --start 2026-08-01 --end 2026-08-31
.venv/bin/python webull_cli.py order detail --account-class INDIVIDUAL_MARGIN --client-order-id ORDER_ID
```

执行记录可通过完整 SDK 入口查询：

```bash
.venv/bin/python webull_cli.py call trade.orders_v3.get_order_executions \
  --account INDIVIDUAL_MARGIN \
  --kwargs '{"account_id":"@account","start_date":"2026-08-01","end_date":"2026-08-31","page_size":20}'
```

## 股票订单

先预览，再下单：

```bash
.venv/bin/python webull_cli.py order preview \
  --symbol AAPL --instrument-type EQUITY --side BUY --quantity 1 \
  --order-type LIMIT --limit-price 100 --tif DAY --session CORE

.venv/bin/python webull_cli.py order place --yes \
  --symbol AAPL --instrument-type EQUITY --side BUY --quantity 1 \
  --order-type LIMIT --limit-price 100 --tif DAY --session CORE \
  --client-order-id MY_UNIQUE_ORDER_ID
```

支持 `MARKET`、`LIMIT`、`STOP_LOSS`、`STOP_LOSS_LIMIT`、`TRAILING_STOP_LOSS`，以及小数股、按金额买入、卖空、日盘/扩展时段/夜盘。特殊字段通过 `--extra` 传入：

```bash
# 追踪止损
.venv/bin/python webull_cli.py order preview \
  --symbol AAPL --instrument-type EQUITY --side SELL --quantity 1 \
  --order-type TRAILING_STOP_LOSS --tif DAY \
  --extra '{"trailing_type":"PERCENTAGE","trailing_stop_step":"0.05"}'

# 按金额买入；total_cash_amount 由官方接口解释
.venv/bin/python webull_cli.py order preview \
  --symbol AAPL --instrument-type EQUITY --side BUY --quantity 0.1 \
  --order-type MARKET --tif DAY --entrust-type AMOUNT \
  --extra '{"total_cash_amount":"20"}'
```

`--session` 可选 `CORE`、`ALL`、`NIGHT`。OTO、OCO、OTOCO、止盈止损组合和算法单使用完整 JSON 的 `--orders @file.json`，不会丢失官方新增字段。

## 期权订单

单腿与多腿策略都通过统一 V3 订单接口。示例：

```bash
.venv/bin/python webull_cli.py order preview \
  --symbol AAPL --instrument-type OPTION --side BUY --quantity 1 \
  --order-type LIMIT --limit-price 2.50 --tif DAY \
  --option-strategy SINGLE \
  --legs '[{"side":"BUY","quantity":"1","symbol":"AAPL","strike_price":"220","option_expire_date":"2026-12-18","instrument_type":"OPTION","option_type":"CALL","market":"US"}]'
```

支持 `SINGLE`、`COVERED_STOCK`、`VERTICAL`、`STRADDLE`、`STRANGLE`、`CALENDAR`、`BUTTERFLY`、`CONDOR`、`COLLAR_WITH_STOCK`、`IRON_BUTTERFLY`、`IRON_CONDOR`、`DIAGONAL` 等策略。卖出期权只能使用 `DAY`；普通期权不支持市价单或追踪止损。具体合约先查询：

```bash
.venv/bin/python webull_cli.py instruments --asset option AAPL --extra '{"page_size":20,"option_type":"CALL"}'
```

## 期货、加密货币和事件合约

```bash
# 期货；把 MNQZ6 替换为标的发现接口返回的当前有效合约
.venv/bin/python webull_cli.py order preview \
  --symbol MNQZ6 --instrument-type FUTURES --side BUY --quantity 1 \
  --order-type LIMIT --limit-price 10000 --tif DAY

# 加密货币。官方接口目前不支持 preview，确认参数后直接在 Sandbox 下单
.venv/bin/python webull_cli.py order place --yes \
  --symbol BTCUSD --instrument-type CRYPTO --side BUY --quantity 0.001 \
  --order-type LIMIT --limit-price 10000 --tif GTC

# 事件合约；必须 LIMIT + DAY，价格必须为 0.01 至 0.99
.venv/bin/python webull_cli.py order preview \
  --symbol EVENT_SYMBOL --instrument-type EVENT --side BUY --quantity 1 \
  --order-type LIMIT --limit-price 0.10 --tif DAY --event-outcome yes
```

加密货币市价单必须用 `IOC`；限价和止损限价单使用 `DAY` 或 `GTC`。事件合约只允许买入开仓和卖出平仓。

发现标的：

```bash
.venv/bin/python webull_cli.py instruments --asset crypto BTCUSD
.venv/bin/python webull_cli.py call data.instruments.get_futures_product_class --args '["US_FUTURES"]'
.venv/bin/python webull_cli.py call data.instruments.get_futures_products --args '["US_FUTURES",2]'
.venv/bin/python webull_cli.py call data.instruments.get_futures_instrument_by_code --args '["MNQ","US_FUTURES"]'
.venv/bin/python webull_cli.py events categories
.venv/bin/python webull_cli.py events series --category ECONOMICS
.venv/bin/python webull_cli.py events events --series KXFEDDECISION
.venv/bin/python webull_cli.py events markets --series KXFEDDECISION
```

## 改单、撤单和批量下单

```bash
.venv/bin/python webull_cli.py order replace --yes \
  --account-class INDIVIDUAL_MARGIN \
  --orders '[{"client_order_id":"ORDER_ID","quantity":"2","limit_price":"101"}]'

.venv/bin/python webull_cli.py order cancel --yes \
  --account-class INDIVIDUAL_MARGIN --client-order-id ORDER_ID

.venv/bin/python webull_cli.py order batch --yes \
  --account-class INDIVIDUAL_MARGIN --batch-orders @batch.json
```

`batch.json` 是最多 50 个完整股票订单组成的 JSON 数组。批量下单目前仅支持股票，而且 Webull 可能需要为账户单独开放该能力。

## 行情

```bash
.venv/bin/python webull_cli.py market snapshot --asset stock AAPL MSFT
.venv/bin/python webull_cli.py market snapshot --asset crypto BTCUSD ETHUSD
.venv/bin/python webull_cli.py market snapshot --asset option AAPL261218C00240000
.venv/bin/python webull_cli.py market bars --asset stock AAPL --timespan D --count 30
.venv/bin/python webull_cli.py market ticks --asset stock AAPL --count 30
.venv/bin/python webull_cli.py market depth --asset stock AAPL --depth 10
```

`--asset` 支持 `stock`、`option`、`futures`、`crypto`、`event`，但并非每类资产都有逐笔或深度接口。Sandbox 默认可返回延迟行情；股票、期权和期货实时行情可能要求额外订阅，加密货币和事件行情无需额外订阅。

行情流和交易事件流：

```bash
.venv/bin/python webull_cli.py stream quotes BTCUSD --category US_CRYPTO --duration 30
.venv/bin/python webull_cli.py stream quotes BTCUSD --category US_CRYPTO --transport websockets --duration 30
.venv/bin/python webull_cli.py stream trades INDIVIDUAL_MARGIN CRYPTO --duration 30
```

省略交易事件流的 `--duration` 会持续运行，按 `Ctrl+C` 停止。

## 全部 SDK 能力

列出全部目标或单个模块的方法签名：

```bash
.venv/bin/python webull_cli.py catalog
.venv/bin/python webull_cli.py catalog --target data.fundamentals
.venv/bin/python webull_cli.py catalog --target data.screener
.venv/bin/python webull_cli.py catalog --target data.watchlist
```

调用任意公开方法：

```bash
.venv/bin/python webull_cli.py call data.instruments.get_company_profile --args '["AAPL"]'
.venv/bin/python webull_cli.py call data.screener.get_market_sectors --args '["US_STOCK"]'
.venv/bin/python webull_cli.py call data.watchlist.get_watchlist
```

账户型接口可以在 JSON 中使用 `"@account"`，再用 `--account` 解析：

```bash
.venv/bin/python webull_cli.py call trade.account_v2.get_account_balance \
  --account INDIVIDUAL_MARGIN --args '["@account"]'
```

所有名称以 `add_`、`create_`、`update_`、`delete_`、`remove_`、`place_`、`replace_`、`cancel_` 或 `batch_place` 开头的通用调用都会要求 `--yes`。例如创建自选列表：

```bash
.venv/bin/python webull_cli.py call data.watchlist.create_watchlist \
  --args '["OpenAPI Test"]' --yes
```

## BTC/ETH 趋势策略

策略只允许连接 Sandbox，覆盖 `BTCUSD` 和 `ETHUSD`：EMA20 上穿 EMA50 且 Heikin-Ashi 上涨时买入；EMA20 下穿 EMA50 或可执行价格较成本价亏损 3% 时全部卖出。每个标的最多一个多头仓位，每次最多使用当前 CRYPTO 账户购买力的 0.5%。

先运行两套固定 90 天回测：

```bash
.venv/bin/python webull_cli.py crypto-strategy backtest --days 90 --source both
```

可独立评估 MPL 2.0 的 SuperTrend 策略（ATR 10、倍数3）；该命令只生成报告，不改变自动交易任务：

```bash
.venv/bin/python webull_cli.py crypto-strategy supertrend-backtest --days 90 --source both
```

报告写入 `reports/crypto-backtest-90d.md` 和 `reports/crypto-backtest-90d.json`。Webull M120 回测按下一根 K 线开盘成交，并测试每边 0.5%、1% 和 1.5% 成本；Coinbase M5 只作高频诊断。只有 Webull M120 在每边 1% 成本下净收益为正、至少 3 笔交易且数据检查通过的标的会被放行。

门槛通过后安装 30 天自动 Sandbox 模拟交易：

```bash
.venv/bin/python webull_cli.py crypto-strategy install --yes
.venv/bin/python webull_cli.py crypto-strategy status
.venv/bin/python webull_cli.py crypto-strategy pause
.venv/bin/python webull_cli.py crypto-strategy resume
.venv/bin/python webull_cli.py crypto-strategy report
.venv/bin/python webull_cli.py crypto-strategy uninstall --yes
```

LaunchAgent 每 60 秒运行一次。止损每分钟检查，趋势信号仅在出现新的已闭合 M120 K 线时处理；市价单固定为 IOC，并使用确定性订单 ID 防止重复提交。连续 3 笔亏损会暂停新开仓 24 小时，但暂停、冷却或到期期间仍允许退出。30 天到期后会尝试市价平仓，确认无持仓及未完成订单后标记完成。电脑必须保持开机并登录；休眠期间不会追补旧入场信号。

LaunchAgent 的最小运行副本、独立虚拟环境、原子 JSON 状态及 JSONL 日志位于 `~/Library/Application Support/WebullCryptoSandbox/`，用于避开 macOS 对 `Documents` 后台访问的限制；回测缓存位于项目 `.cache/`。这些运行数据都不会提交 Git，30 天汇总写入 `reports/crypto-experiment.md`。可手动执行单轮诊断，但仍需显式确认：

```bash
.venv/bin/python webull_cli.py crypto-strategy run-once --yes
```

## Sandbox 日内交易器

日内模式使用已闭合的五分钟K线：EMA20高于EMA100且收盘价突破此前48根K线高点时买入；跌破EMA20、亏损2%、盈利5%或持仓满24小时后卖出。只做多，每次使用当前CRYPTO购买力的0.1%，全账户最多一个仓位，每个标的每天最多入场一次、全账户每天最多两次；当天两笔亏损后停止新开仓。暂停只阻止新开仓，不阻止退出。

先生成90天回测。报告会明确显示零成本、每边0.25%和Webull每边1%成本下的结果；Sandbox运行属于学习实验，不代表策略通过盈利门槛：

```bash
.venv/bin/python webull_cli.py crypto-strategy daytrade-backtest --days 90
```

安装后会替换同一标签下原来的M120 LaunchAgent，并把原策略设为暂停；原状态和日志不会删除。由于当前Sandbox MQTT在TCP和WebSocket下均返回`101 Internal error`，日内模式使用已验证可用的HTTP接口：每5秒查询一次BTC/ETH快照，仅在新五分钟K线闭合后计算信号。频率低于官方Sandbox每个相关接口30次/60秒的限制。

```bash
.venv/bin/python webull_cli.py crypto-strategy daytrade-install --yes
.venv/bin/python webull_cli.py crypto-strategy daytrade-status
.venv/bin/python webull_cli.py crypto-strategy daytrade-pause
.venv/bin/python webull_cli.py crypto-strategy daytrade-resume
.venv/bin/python webull_cli.py crypto-strategy daytrade-report
.venv/bin/python webull_cli.py crypto-strategy daytrade-uninstall --yes
```

手动执行一轮或在终端前台持续运行：

```bash
.venv/bin/python webull_cli.py crypto-strategy daytrade-run-once --yes
.venv/bin/python webull_cli.py crypto-strategy daytrade-run --yes
```

## 测试

```bash
.venv/bin/python -m unittest -v
.venv/bin/python -m compileall -q webull_api.py webull_orders.py webull_cli.py webull_streams.py crypto_strategy.py crypto_runtime.py daytrader_strategy.py daytrader_runtime.py
```

已知环境差异：Webull Sandbox 的 `/trade/calendar` 当前返回 404；当前模拟账户的批量下单开关未开放；交易事件 gRPC 已连接成功，但 Sandbox MQTT 行情流本次在 TCP 和 WebSocket 下均返回 `101 Internal error`。这些能力仍完整保留在 CLI 中，服务端开关或状态恢复后无需改代码。若遇到 `429 TOO_MANY_REQUESTS`，等待接口限流窗口恢复后重试。

官方资料：[SDK](https://developer.webull.com/apis/docs/sdk/)、[Trading API](https://developer.webull.com/apis/docs/trade-api/overview/)、[Market Data API](https://developer.webull.com/apis/docs/market-data/overview/)。
