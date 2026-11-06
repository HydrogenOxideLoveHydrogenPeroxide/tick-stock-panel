# 本地量化扩展与配套策略依赖

本分支包含量化助手、手工成交记账、公开财报/市值采集、研究因子和回测接入。新增策略复用配套 `quant-rsi` 库；不要仅复制当前面板目录后假定所有研究数据已经包含。

## 固定的配套代码版本

- 仓库：https://github.com/HydrogenOxideLoveHydrogenPeroxide/quant-rsi
- 分支：`tick-panel-dependencies-20261106`
- 固定提交：`771aa7dfc053a21d2767554440ae86ae1b415213`

目录应并列放置：

```text
workspace/
  tick-stock-panel/
  quant-rsi/
```

在workspace目录准备配套库：

```bash
git clone --branch tick-panel-dependencies-20261106 https://github.com/HydrogenOxideLoveHydrogenPeroxide/quant-rsi.git quant-rsi
git -C quant-rsi checkout --detach 771aa7dfc053a21d2767554440ae86ae1b415213
cd quant-rsi
./quant/setup.sh
```

如果已有quant-rsi工作目录，请使用独立目录/工作树核对这个版本，避免覆盖自己的未提交修改。面板Python与前端依赖按原README及 `LOCAL_HARDENING.md` 准备。本机的 `backend/requirements-local.lock` 为加固运行环境依赖快照。

面板使用配套库的六个策略/因子模块及既有公开财报CLI。该配套提交只增加必要模块与对应测试，不包含当前机器的机器人配置、研究中的ML代码、行情缓存或持仓。

## 数据边界

GitHub只发布源码、测试与说明，不上传 `data/`、`.run/`、`research/`、Python环境、原始财报、持仓与缓存。依赖代码准备好后可在量化助手中主动采集当前公开三表、市值与行业，详见 `PUBLIC_FUNDAMENTAL_SOURCES.md`。

长历史回测使用的 `data/backtest_history/manifest.json` 和经SHA256校验的分区归档需要另行准备；没有这些数据时相关研究模型会明确阻断，不能用空数据伪造收益。历史PIT财报与总市值仍需另外核验。部分涉及真实研究归档的测试会在缺归档时跳过，其余合成样本和接口测试可独立运行。

当前扩展以本地部署为目标；上游Docker工作流不会自动打包外置的quant-rsi目录或本机研究档案。新环境请按上述本地依赖布局运行。
