"""Read-only historical archive for raw-price backtests; current local rows win overlaps."""
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import polars as pl

SCHEMA = {"symbol": pl.String, "date": pl.Date,
          **{key: pl.Float64 for key in ("open", "high", "low", "close", "volume", "amount")}}


@lru_cache(maxsize=4)
def _manifest(root: str, modified: int):
    folder = Path(root)
    data = json.loads((folder / "manifest.json").read_text())
    if not data.get("complete"):
        raise ValueError("回测长历史归档不完整")
    for record in data["files"] + [data["index"]]:
        name = record["file"]
        if Path(name).name != name or not name.endswith(".parquet"):
            raise ValueError("回测历史文件名无效")
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError("回测长历史归档指纹不匹配")
    return data


class BacktestHistoryRepository:
    def __init__(self, repo):
        self.local = repo
        self.root = Path(repo.store.data_dir) / "backtest_history"

    def __getattr__(self, name):
        return getattr(self.local, name)

    def archive(self):
        file = self.root / "manifest.json"
        return _manifest(str(self.root), file.stat().st_mtime_ns) if file.exists() else None

    def _read(self, records, start, end, symbols=None):
        frame = pl.scan_parquet([str(self.root / item["file"]) for item in records], schema=SCHEMA,
            extra_columns="ignore", missing_columns="insert",
            cast_options=pl.ScanCastOptions(integer_cast="allow-float", float_cast="upcast"))
        frame = frame.filter(pl.col("date").is_between(start, end))
        if symbols is not None:
            frame = frame.filter(pl.col("symbol").is_in(symbols))
        return frame.collect(engine="streaming")

    @staticmethod
    def _merge(archived, local):
        if archived.is_empty():
            return local
        if local.is_empty():
            return archived.sort(["symbol", "date"])
        return pl.concat([archived, local], how="diagonal_relaxed").unique(
            subset=["symbol", "date"], keep="last", maintain_order=True).sort(["symbol", "date"])

    def get_raw_daily_range(self, start, end, symbols=None):
        local = self.local.get_raw_daily_range(start, end, symbols=symbols)
        manifest = self.archive()
        if not manifest:
            return local
        archived = self._read(manifest["files"], start, end, symbols)
        return self._merge(archived, local)

    def get_index_daily(self, symbol, start, end, columns=None):
        local = self.local.get_index_daily(symbol, start, end)
        manifest = self.archive()
        if manifest:
            archived = self._read([manifest["index"]], start, end, [symbol])
            local = self._merge(archived, local)
        if columns is not None and not local.is_empty():
            local = local.select([key for key in columns if key in local.columns])
        return local

    def history_info(self, raw=False):
        kind = "kline_daily" if raw else "kline_daily_enriched"
        files = list((Path(self.local.store.data_dir) / kind).glob("**/*.parquet"))
        start = end = None
        if files:
            bounds = pl.scan_parquet(files, schema={"date": pl.Date}, extra_columns="ignore").select(pl.col("date").min().alias("start"), pl.col("date").max().alias("end")).collect().row(0)
            start, end = bounds
        manifest = self.archive() if raw else None
        if manifest:
            from datetime import date
            first, last = date.fromisoformat(manifest["start"]), date.fromisoformat(manifest["end"])
            start, end = min(start, first) if start else first, max(end, last) if end else last
        return dict(earliest_date=str(start) if start else None, latest_date=str(end) if end else None,
            archive_available=manifest is not None,
            source="本地原始日线 + 长历史研究归档" if manifest else "本地原始日线" if raw else "本地指标日线",
            note="研究归档为当前证券池回溯、不复权；非完整历史上市/退市股票池。" if manifest else "")
