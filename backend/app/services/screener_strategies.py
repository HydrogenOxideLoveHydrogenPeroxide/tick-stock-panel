"""Shared strategy directory and execution for Screener and Quant Assistant."""
from app.services.screener import PRESET_STRATEGIES, ScreenerService
from app.strategy import config as strategy_config


def list_screener_strategies(repo, engine=None):
    data_dir = repo.store.data_dir
    presets, seen = [], set()
    definitions = [dict(meta, id=sid, source="builtin") for sid, meta in PRESET_STRATEGIES.items()]
    if engine:
        definitions.extend(engine.list_strategies())
    for meta in definitions:
        sid = meta["id"]
        if sid in seen:
            continue
        overrides = strategy_config.load_override(data_dir, sid)
        presets.append(dict(id=sid, name=overrides.get("name") or meta["name"],
                            description=overrides.get("description") or meta.get("description", ""),
                            source=meta.get("source", "custom")))
        seen.add(sid)
    return {"presets": presets, "load_errors": engine.load_errors() if engine else []}


def run_screener_strategy(repo, engine, strategy_id, as_of, pool=None):
    """Use exactly the same filters, saved overrides and result limit in both pages."""
    overrides = strategy_config.load_override(repo.store.data_dir, strategy_id)
    limit = overrides.get("display_limit")
    if limit is None and "display_limit" in overrides:
        limit = 0
    if strategy_id in PRESET_STRATEGIES:
        result = ScreenerService(repo).run_preset(strategy_id, as_of=as_of, pool=pool,
                                                basic_filter=overrides.get("basic_filter"), display_limit=limit)
    else:
        if engine is None or not engine.has(strategy_id):
            raise ValueError("策略不存在或尚未加载，请重新同步选股页策略")
        result = engine.run(strategy_id, as_of, pool=pool, overrides=overrides or None)
        if limit is not None and limit > 0:
            result.rows = result.rows[:limit]
            result.total = min(result.total, limit)
    return result, overrides
