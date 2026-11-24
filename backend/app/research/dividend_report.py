"""Portable report artifacts from actual monthly portfolio simulation results."""
from __future__ import annotations
from datetime import datetime
import html
import json
from pathlib import Path
from app.research.dividend_simulator import Config,run
from app.services.public_financials import atomic_json,SHANGHAI


def pct(v):return '—' if v is None else f'{100*v:,.2f}%'


def build(folder):
    root=Path(folder);reports=root/'report';reports.mkdir(exist_ok=True)
    base=run(root,Config())
    scenarios={'10万元·基准':base}
    for name,config in [('100万元·容量与成本',Config(initial_cash=1000000.)),
                        ('单边滑点20bp',Config(slippage_bps=20.)),
                        ('分红税前对照',Config(dividend_tax=0.))]:
        result=run(root,config)
        scenarios[name]=result
    atomic_json(reports/'result.json',base)
    atomic_json(reports/'sensitivity.json',{name:{'config':r['config'],'stats':r['stats'],'data_quality':r['data_quality']} for name,r in scenarios.items()})
    # Standalone CSV order ledger/equity/targets for inspection, no real holdings.
    import csv
    for filename,rows in [('orders.csv',base['trades']),('equity.csv',base['curve'])]:
        with (reports/filename).open('w',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=list(rows[0]))
            writer.writeheader();writer.writerows({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in row.items()} for row in rows)
    chart(base,reports/'performance.png')
    stats=base['stats'];quality=base['data_quality'];labels={'000300.SH':'沪深300','000852.SH':'中证1000','000001.SH':'上证综指'}
    benchmarks=''.join(f'<tr><td>{labels[c]}价格指数</td><td>{pct(b["stats"]["total_return"])}</td><td>{pct(b["stats"]["annual_return"])}</td><td>{pct(b["stats"]["max_drawdown"])}</td></tr>' for c,b in base['benchmarks'].items())
    years=''.join(f'<tr><td>{year}</td><td>{pct(value)}</td>'+''.join(f'<td>{pct(base["benchmarks"][code]["stats"]["yearly_returns"].get(year))}</td>' for code in labels)+'</tr>' for year,value in stats['yearly_returns'].items())
    sensitivity=''.join(f'<tr><td>{html.escape(name)}</td><td>{pct(r["stats"]["total_return"])}</td><td>{pct(r["stats"]["annual_return"])}</td><td>{pct(r["stats"]["max_drawdown"])}</td><td>{r["stats"]["fees"].get("total",0):,.2f}</td></tr>' for name,r in scenarios.items())
    monthly=__import__('app.research.dividend_history',fromlist=['read']).read(root/'targets.json')['rows']
    holdings=''.join('<tr><td>'+m['trade_date']+'</td><td>'+', '.join(html.escape(r['symbol']) for r in m['selected'])+'</td><td>'+str(len(m['missing_capital']))+'</td></tr>' for m in monthly)
    cash_tax=stats['dividend_tax_provision'];fees_=stats['fees']
    text=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>菜场大妈 · 历史重建研究回测</title>
<style>body{{margin:0;background:#0b1220;color:#e5e7eb;font:16px/1.7 system-ui,sans-serif}}main{{max-width:1120px;margin:auto;padding:36px 24px}}h1{{font-size:34px;margin:8px 0}}h2{{margin-top:36px}}p{{color:#bac6d8}}.badge{{color:#fbbf24}}.notice{{border-left:4px solid #fbbf24;background:#211c16;padding:18px}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin:28px 0}}.card{{background:#152238;padding:20px;border-radius:12px}}.card b{{display:block;font-size:27px}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{text-align:left;padding:12px;border-bottom:1px solid #293449}}.scroll{{overflow:auto}}a{{color:#7dd3fc}}img{{width:100%;border-radius:12px}}small{{color:#94a3b8}}li{{margin:8px 0}}details{{margin:24px 0}}footer{{margin-top:40px;color:#94a3b8}}</style>
<main><div class="badge">公开历史数据重建 · 固定规则 · 未优化参数</div><h1>菜场大妈策略，历史表现如何？</h1>
<p>{base['curve'][0]['date']} — {base['curve'][-1]['date']} · 初始资金10万元 · {quality['months']}次月度选股</p>
<div class="notice"><b>这不是原始公告版本已核验的严格 PIT 回测。</b>财报按历史公告日期对齐，但来源提供当前修订值。所选 {quality['selected_slots']} 个“股票×月份”中，{quality['later_revised_selected']} 个记录的来源更新时间晚于信号日，原始利润值仍需核验。历史股本只采用公告日、生效日均不晚于信号日的记录；缺公告时间不以今天的股本补齐。结果仅用于研究，不足以确认可复制的实盘收益。</div>
<div class="cards"><div class="card">累计收益<b>{pct(stats['total_return'])}</b></div><div class="card">年化收益<b>{pct(stats['annual_return'])}</b></div><div class="card">收盘最大回撤<b>{pct(stats['max_drawdown'])}</b></div><div class="card">夏普比率（rf=0）<b>{stats['sharpe_rf0']:.2f}</b></div></div>
<img src="performance.png" alt="净值、回撤与年度收益图">
<p>最大回撤对应前高{stats['max_drawdown_period'].get('peak_date')}，低点{stats['max_drawdown_period'].get('trough_date')}；恢复前高日期为{stats['max_drawdown_period'].get('recovery_date') or '截至期末尚未恢复'}。</p>
<h2>规则与成交假设</h2><ul><li>沪深主板普通A股；上市满250个交易日；在信号日逐日核验非ST、非停牌；纳入后来退市的公司，未以今天仍上市为条件筛选。</li><li>月末不复权收盘价2～9元；过去12个月已实施每股现金分红/该价格≥3%；最近已公告归母净利润大于0。用当时可知总股本乘不复权价格，按总市值升序、代码升序选前10只。</li><li>下月第一个交易日开盘执行，目标每只占前收盘总资产的10%；不足10只保留现金，不融资、不做空。100股整手买入；旧持仓及送转产生的零股可在全部退出时卖出。</li><li>单边佣金万3，最低5元；过户费按2022-04-29前后费率；卖出印花税在2023-08-28起从千1降至万5；单边滑点5bp。</li><li>单日委托不超过此前最多20个有成交交易日平均成交量的5%。开盘涨停保守不买入；开盘跌停/停牌保守不卖出，旧持仓后续交易日继续尝试退出；被阻买入的资金留到下一次月度调仓。</li><li>股权登记日收盘持仓才享有分红；除权日确认现金应收和送股，现金实际派发后才能用于买入，送股在上市日后才能卖出。不使用复权价重复计分红。</li><li>现金分红按20%统一保守计提税款，未按实际持有期限逐笔结算；税前对照另列。送股税和配股参与未单独模拟，未计现金利息。期末持仓按收盘估值，未强制卖出。</li></ul>
<h2>基准对比</h2><p>指数对照为价格指数，不含现金分红；策略包含现金分红并计提税费。两者口径有差异，超额表现不能全部解释为选股能力。</p><div class="scroll"><table><tr><th>对象</th><th>累计收益</th><th>年化收益</th><th>最大回撤</th></tr><tr><td>菜场大妈（10万元）</td><td>{pct(stats['total_return'])}</td><td>{pct(stats['annual_return'])}</td><td>{pct(stats['max_drawdown'])}</td></tr>{benchmarks}</table></div>
<h2>分年度表现</h2><small>2026年截至{base['curve'][-1]['date']}，不是完整年度。</small><div class="scroll"><table><tr><th>年度</th><th>策略</th><th>沪深300</th><th>中证1000</th><th>上证综指</th></tr>{years}</table></div>
<h2>成本与容量敏感性</h2><div class="scroll"><table><tr><th>情景</th><th>累计收益</th><th>年化</th><th>最大回撤</th><th>佣金、印花税及过户费/元</th></tr>{sensitivity}</table></div>
<p>基准共发生{stats['trades']}笔成交委托、{stats['blocked_orders']}次被阻委托。显式交易费用{fees_.get('total',0):,.2f}元；价格中另计滑点成本{fees_.get('slippage',0):,.2f}元；现金分红税款计提{cash_tax:,.2f}元。税前对照保留其他费用，用来观察统一税率假设的影响。</p>
<h2>数据覆盖与可复核边界</h2><ul><li>缺财报候选累计{quality['missing_financial_candidates']}个，缺时点股本候选累计{quality['missing_capital_candidates']}个（按股票×月份计，非独立股票数）。缺数据候选未按合格处理。另有{quality['deferred_profit_candidates_below_verified_top20']}个退市股票候选（股票×月份）未补归母利润，但其市值排序已在核验后的前20名之外，故不影响前10；若可能进入前20，程序会停止而不是假定盈利。需与完整商业PIT数据对照。</li><li>IPO/退市日期、逐日ST/停牌和原始价格来自BaoStock；存续股票的股本公告和变动记录来自巨潮；退市股票在巨潮无记录时使用BaoStock当时已披露的季末总股本，未完整重建这些公司的期内股本变动；财报与已实施分红来自东方财富。现金派发日和送股上市日由BaoStock补充。存续公司每股分红与东方财富交叉核对；已退市公司的历史分红另从BaoStock补齐，未声称独立双源核验。</li><li>缺失持仓日线、股本时间冲突、分红金额或除权事件不一致时停止计算；没有用空收益填补缺口。原始输入保留采集时间和SHA256指纹。</li><li>历史分红股票池共{quality['historical_dividend_universe_symbols']}只，其中包含{quality['delisted_symbols_in_universe']}只后来退市的股票；缺证券基本资料{len(quality['missing_basic_symbols'])}只。历史状态库仍由公开服务当前版本重建；未逐份核验历史原始财报、交易所退市整理公告和数据后续修订。全样本表现不能视为严格样本外收益。</li></ul>
<details><summary>查看每月目标股票</summary><div class="scroll"><table><tr><th>交易日</th><th>目标名单</th><th>缺股本候选</th></tr>{holdings}</table></div></details>
<h2>导出核查</h2><p><a href="result.json">完整计算结果 JSON</a> · <a href="sensitivity.json">情景对照</a> · <a href="orders.csv">逐笔委托 CSV</a> · <a href="equity.csv">每日净值 CSV</a></p>
<footer>生成于 {datetime.now(SHANGHAI).isoformat()}。该报告使用模拟资金与公开数据，不包含真实持仓或交易。</footer></main></html>'''
    (reports/'index.html').write_text(text)
    return base


def chart(result,path):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    dates=[datetime.fromisoformat(r['date']) for r in result['curve']]
    initial=result['config']['initial_cash'];equity=[r['equity']/initial for r in result['curve']]
    fig,axes=plt.subplots(3,1,figsize=(12,10),gridspec_kw={'height_ratios':[2.2,1,1.2]},constrained_layout=True)
    fig.patch.set_facecolor('#f8fafc')
    axes[0].plot(dates,equity,label='Dividend / low price / small cap',color='#2563eb',linewidth=1.7)
    labels={'000300.SH':'CSI 300 (price)','000852.SH':'CSI 1000 (price)','000001.SH':'SSE Composite (price)'}
    for code,b in result['benchmarks'].items():axes[0].plot(dates,[r['equity']/initial for r in b['curve']],label=labels[code],alpha=.7,linewidth=1)
    axes[0].set_yscale('log');axes[0].set_yticks([.5,1,2,3,4,5],labels=['0.5','1','2','3','4','5']);axes[0].minorticks_off();axes[0].set_ylabel('Wealth / initial cash (log scale)');axes[0].legend(loc='upper left',fontsize=8)
    peak=1.;drawdowns=[]
    for value in equity:peak=max(peak,value);drawdowns.append(100*(value/peak-1))
    axes[1].fill_between(dates,drawdowns,0,color='#ef4444',alpha=.35);axes[1].set_ylabel('Drawdown (%)')
    years=list(result['stats']['yearly_returns']);positions=list(range(len(years)));width=.22
    axes[2].bar([x-width for x in positions],[result['stats']['yearly_returns'][y]*100 for y in years],width,label='Strategy')
    for j,code in enumerate(['000300.SH','000852.SH']):axes[2].bar([x+j*width for x in positions],[result['benchmarks'][code]['stats']['yearly_returns'].get(y,0)*100 for y in years],width,label=labels[code])
    axes[2].set_xticks(positions,[y+'*' if y==dates[-1].strftime('%Y') else y for y in years]);axes[2].set_xlabel('* '+dates[-1].strftime('%Y')+' through '+dates[-1].strftime('%Y-%m-%d'));axes[2].set_ylabel('Year return (%)');axes[2].legend(fontsize=8)
    for ax in axes:ax.grid(alpha=.2);ax.set_axisbelow(True)
    for ax in axes[:2]:ax.xaxis.set_major_locator(mdates.YearLocator());ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y'))
    fig.suptitle('Monthly dividend / low-price / small-cap research backtest\nPublic historical reconstruction; original filing versions not verified',fontsize=13)
    fig.savefig(path,dpi=160);plt.close(fig)
