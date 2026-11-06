"""Quant Assistant JSON endpoints; saving and analysis are intentionally separate."""
import asyncio
from fastapi import APIRouter, HTTPException, Request
from app.services.quant_assistant import (
    Portfolio, SuggestRequest, PortfolioStorageError, TradeRequest, TradeConflict, load_trades, record_trade,
    get_screener_catalog, load_portfolio, save_portfolio, suggest_portfolio,
)

router = APIRouter(prefix="/api/quant-assistant", tags=["quant-assistant"])


@router.get("/portfolio", response_model=Portfolio)
async def get_portfolio():
    try:
        return await asyncio.to_thread(load_portfolio)
    except PortfolioStorageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.put("/portfolio", response_model=Portfolio)
async def put_portfolio(request: Request, portfolio: Portfolio):
    if portfolio.strategy.screener_strategy_id:
        catalog = get_screener_catalog(request.app.state.repo, getattr(request.app.state, "strategy_engine", None))
        if portfolio.strategy.screener_strategy_id not in {s["id"] for s in catalog["strategies"]}:
            raise HTTPException(status_code=422, detail="所选策略已不可用，请重新同步选股页策略")
    try:
        return await asyncio.to_thread(save_portfolio, portfolio)
    except PortfolioStorageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/strategies")
async def strategies(request: Request):
    return await asyncio.to_thread(get_screener_catalog, request.app.state.repo, getattr(request.app.state, "strategy_engine", None))


@router.post("/suggest")
async def suggest(request: Request, payload: SuggestRequest):
    return await suggest_portfolio(request.app.state.repo, payload.portfolio, payload.mode,
                                   strategy_engine=getattr(request.app.state, "strategy_engine", None))


@router.get("/trades")
async def trades():
    try:
        return await asyncio.to_thread(load_trades)
    except PortfolioStorageError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/trades")
async def post_trade(payload: TradeRequest):
    try:
        return await asyncio.to_thread(record_trade, payload)
    except (PortfolioStorageError, TradeConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/research-context")
async def research_context():
    from app.services.context_strategy_status import context_strategy_status
    return await asyncio.to_thread(context_strategy_status)


@router.post("/candidates")
async def candidates(request: Request, payload: SuggestRequest):
    from app.services.quant_assistant import screen_candidates
    try:
        return await screen_candidates(request.app.state.repo, getattr(request.app.state, "strategy_engine", None), payload)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(504, detail="选股计算超时，请稍后重试") from exc


@router.post("/explain")
async def explain(request: Request, payload: SuggestRequest):
    from app.services.quant_assistant import explain_portfolio
    try:
        return await explain_portfolio(request.app.state.repo, getattr(request.app.state, "strategy_engine", None), payload)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    except Exception as exc:
        # Provider exceptions may contain credential-bearing request details.
        raise HTTPException(502, detail="LLM 解读失败或超时，请检查模型配置；组合未修改") from exc


from datetime import datetime
from pydantic import BaseModel, Field
from app.services.buffett_factors import SHANGHAI, load_dataset, evaluate_dataset

class ValueFactorRequest(BaseModel):
    decision_at: datetime | None = None
    symbols: list[str] | None = Field(default=None, max_length=1000)


@router.post("/value-factors/evaluate")
async def value_factor_snapshot(request: Request, payload: ValueFactorRequest):
    now = datetime.now(SHANGHAI)
    decision = payload.decision_at or now
    if decision.tzinfo is None or decision.utcoffset() is None or decision > now:
        raise HTTPException(422, detail="决策时间必须带时区且不得在未来")
    try:
        data = await asyncio.to_thread(load_dataset, request.app.state.repo.store.data_dir)
        return await asyncio.to_thread(evaluate_dataset, data, decision, payload.symbols)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


from app.services.fundamental_sync import SyncRequest, start_collect, sync_status

@router.get('/value-factors/sync')
async def value_sync_status(request: Request):
    return await asyncio.to_thread(sync_status, request.app.state.repo.store.data_dir)


@router.post('/value-factors/sync')
async def value_sync(request: Request, payload: SyncRequest):
    try:
        return await asyncio.to_thread(start_collect, request.app.state.repo.store.data_dir, payload.symbols)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
