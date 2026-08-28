# Webull Sandbox 策略研究尝试账本

状态：2026-08-29冻结。目的不是挑选看起来最好的历史结果，而是完整登记所有尝试，防止多重试验、保留样本复用和失败遗忘。

## 当前结论

- 已评估12个固定策略规格，属于9个策略家族。
- 当前机构化授权下，可部署策略为0。
- 7个规格已经消费过当时定义的样本外或保留窗口；2个加密诊断没有独立样本外；3个股票规格因开发Gate失败而没有请求保留窗口。
- 原始EMA/Heikin-Ashi加密趋势策略只通过早期宽松Gate；它不满足后来冻结的机构化授权书，因此旧任务保持暂停。
- 当前20日股票前向数据仅用于执行质量和TCA，不是价格信号的样本外收益数据。

## 完整账本

| 规格 | 家族 | 资产 | 当前结论 | 保留样本 | 首要失败原因 |
|---|---|---|---|---|---|
| `crypto_ema_ha_m120` | EMA/HA趋势 | 加密 | 仅旧Gate | 非独立 | 无独立样本外；压力成本为负，M5诊断严重亏损 |
| `crypto_supertrend_atr10x3` | SuperTrend | 加密 | 不交易诊断 | 非独立 | 基准成本亏损，低成本也大致落后持有 |
| `crypto_m5_trend_breakout` | M5突破 | 加密 | `NO_TRADE` | 已消费 | 样本外、成本、超额和稳定性失败 |
| `equity_orb_m5` | ORB | 股票 | `NO_TRADE` | 已消费 | 样本外为负、落后QQQ、压力成本失败 |
| `equity_orb_m1_holdout` | ORB | 股票 | `NO_TRADE` | 已消费 | M1复核仍亏损，质量Gate也失败 |
| `equity_closing_half_hour_momentum` | 收盘动量 | 股票 | `NO_TRADE` | 已消费 | 期望、Sharpe、超额和稳定性失败 |
| `equity_noise_area_vwap` | Noise-Area/VWAP | 股票 | `NO_TRADE` | 已消费 | 利润因子、显著性、稳定性和双向一致性失败 |
| `equity_spy_ivv_relative_value` | 相对价值 | 股票 | `NO_TRADE` | 已消费 | 仅零成本为正，基础成本即失败 |
| `equity_opening_pressure_reversal` | 开盘压力 | 股票 | `NO_TRADE` | 已消费 | 零成本即亏损且多项风险Gate失败 |
| `equity_opening_pressure_momentum` | 开盘压力 | 股票 | 开发拒绝 | 未请求 | 污染样本上基础成本、期望和稳定性失败 |
| `equity_classic_sector_momentum` | 行业动量 | 股票 | 开发拒绝 | 未请求 | 数据质量Gate失败 |
| `equity_intermediate_sector_momentum` | 行业动量 | 股票 | 开发拒绝 | 未请求 | 未跑赢行业等权，风险调整与显著性失败 |

## 下一轮研究预算

前向数据达到`DATA_USABLE`后，只允许预注册一个新的策略家族：

1. 经济假设、唯一参数、成本、基准、风险和失败条件必须先冻结；不允许参数网格搜索。
2. 已查看过的历史窗口只能标记为开发样本，不能再次称为样本外。
3. 未请求的保留窗口一旦用于某个规格即被消费，不得同时替多个候选背书。
4. 最终证据必须包含20个完整交易日、至少100个候选订单的无下单影子账本。
5. 任何Gate失败，本轮研究预算归零；不得换一个小参数继续试到通过。

机器可读原始账本为`reports/research-attempt-ledger.json`。

