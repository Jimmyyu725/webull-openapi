# Webull股票Sandbox 前向执行数据协议

状态：2026-08-29冻结；任何正式记录写入前完成。该协议只验证数据与执行可观测性，不是交易策略，也不授权下单。

## 决策问题

Webull Sandbox 的股票实时快照与一分钟K线，是否足以支持以后对日内策略进行可信的、完全前向的成本和执行验证？

## 固定范围

- 标的固定为`SPY`、`QQQ`、`AAPL`，分别代表高流动性宽基ETF、科技ETF和大型单股。
- 每60秒运行一次，只接受美东时间09:30至16:00的常规交易时段数据。
- 每个标的、每根已经闭合的M1 K线最多写入一条记录；网络重试和进程重启不得重复写入。
- 记录目标为20个完整普通交易日。某日每个标的至少371个唯一分钟样本，才算完整交易日；半日市不计入20日目标。
- 数据仅写入`~/Library/Application Support/WebullEquityForward/`，不提交Git。

## 每条记录

- 本地请求时间、标的、闭合K线时间和Webull交易时段标签。
- M1的开、高、低、收、成交量。
- 最新价、买一、卖一、买一量、卖一量、Webull报价时间。
- 中间价、完整报价点差（基点）、报价年龄（秒）和质量标记。

## 质量Gate

20日结束后，三个标的必须分别同时满足：

- 20个完整普通交易日，且每日至少371个唯一分钟样本。
- K线时间严格递增，无重复；每条记录仅使用已经闭合的K线。
- 有效买卖报价覆盖率至少99%。
- 报价年龄的95分位数不超过5秒。
- 完整报价点差的95分位数不超过5个基点。
- 运行日志没有订单提交事件；记录器代码不引用任何下单接口。

Gate通过只说明前向数据可用于后续研究，不说明任何策略有优势。新策略仍必须独立预注册、通过成本后开发样本与未见样本，并完成无下单影子观察。

## 运行与停止

- `forward-record-once`执行一次只读采样。
- 独立LaunchAgent标签为`com.jingtianyu.webull-equity-forward`，与现有加密任务隔离。
- `forward-record-status`只读汇总当前样本和质量指标。
- `forward-record-install --yes`与`forward-record-uninstall --yes`安装或移除本地每分钟任务。
- 达到20个完整交易日后自动停止写入；用户可随时卸载。

## 明确排除

- 不使用超过一档的订单簿；Webull当前股票接口拒绝`depth > 1`。
- 不回填安装前的历史快照，不用后见数据伪造前向记录。
- 不计算策略收益、不调参、不创建模拟订单、不修改任何账户或现有持仓。

## 依据

- [Webull Market Data Getting Started](https://developer.webull.com/apis/docs/market-data-api/getting-started/)
- [Webull Stock Historical Bars](https://developer.webull.com/apis/docs/reference/broker-market-data-api/bars-using-get/)
- [Nagel, Evaporating Liquidity](https://academic.oup.com/rfs/article-abstract/25/7/2005/1602153)
