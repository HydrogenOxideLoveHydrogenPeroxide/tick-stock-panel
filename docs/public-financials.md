# 公开财务与分红数据

财务数据默认使用 `FINANCIAL_DATA_PROVIDER=eastmoney`，不要求 TickFlow
Expert。行情仍沿用项目现有 TickFlow 适配。可设为 `tickflow` 恢复官方财务接口。

在「财务分析」点击「全部同步」：依次同步核心指标、利润表、资产负债表、
现金流量表，再采集菜场大妈当前收盘快照。也可单独同步利润表后点击
「同步分红筛选快照」。任务在后台运行；失败原因显示在页面，旧数据保留。
公开来源可能限流/变更，项目设有超时、重试、大小/分页上限，禁止执行 JSONP/JS。

## GitHub 来源与许可

接口协议参考 AKShare，MIT，固定审查版本：
`ccc2a488964a6d42f1240b94880c5630a1201f31`。

- [业绩报表](https://github.com/akfamily/akshare/blob/ccc2a488964a6d42f1240b94880c5630a1201f31/akshare/stock/finance/stock_yjbb_em.py)
- [三大报表](https://github.com/akfamily/akshare/blob/ccc2a488964a6d42f1240b94880c5630a1201f31/akshare/stock/finance/stock_three_report_em.py)
- [分红送配](https://github.com/akfamily/akshare/blob/ccc2a488964a6d42f1240b94880c5630a1201f31/akshare/stock/issue/stock_fhps_em.py)

本项目用 httpx/Polars 重写有限字段适配，避免增加 AKShare 全量依赖；协议有
字段名校验，不依赖返回列顺序，不下载/执行远程代码。
许可保留在 `third_party/akshare/LICENSE`。
三大报表使用同源数据中心 `RPT_DMSK_FN_*` 报告；指标为 `RPT_LICO_FN_CPD`，
分红为 `RPT_SHAREBONUS_DET`。缺少的科目保留 null，不用其他科目猜测替代。
金额单位为人民币元，ROE/增长率/毛利率为百分点，利润与现金流为年初累计。

## 时间与数据口径

财务同步取得每类最新报告期的全市场可用记录。后续同步保留旧报告期、替换
同一期当前修订值，并将每批原始标准化观测归档在
`data/financials/archives/`。每张表旁 `.source.json` 保存来源、采集时间及局限。
这不是历史 PIT 原始版本库；披露日期不等于我们实际取得该版本的日期。

菜场大妈 `ai_mwum51zl` 仅支持采集当日的当前收盘筛选：

- 当日沪深主板名单、已公布且归母净利润为正的最新利润表。
- 按除权除息日过滤过去12个月内已实施方案，预案、未来实施方案排除。
- 现金分红元/10股先除10，再按窗口内后续送转比例换算每股金额。
- 腾讯字段45提供当前总市值（亿元）；同时点市值/价格推导当前股本，
  不冒充历史股本。股价、上市天数等缺失时不默认为合格。
- ST/退市整理按当日名称识别，无成交股票保守剔除；它们不构成历史状态库。
- 上市交易日来自已有上证综指（000001.SH）交易日期，窗口内计数为老股票天数下界；
  至少250个交易日且覆盖当日才可验证门槛。
- 盘中不将最新价冒充收盘价；15:10后、报价日期与交易日历一致时才生成快照。

快照独立保存于 `data/fundamentals/dividend_screen/`，不会将今日数据并入历史
K线。历史日期查询、过期快照、历史回测均明确拒绝。月度等权组合撮合尚未实现；
当前候选列表不能当作月度策略的回测结果。

## 命令行

在 backend 环境中执行 `python scripts/sync_public_financials.py --table all`。
可选 `--table income` 或 `--table dividend_screen`，以及 `--data-dir /path/to/data`。

代码和测试可提交 GitHub；运行数据、密钥、cookies、.env、SSH 文件、备份不得提交。
