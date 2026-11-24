"""Collect audited public historical inputs and run monthly dividend research."""
import argparse
from datetime import date
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import settings
from app.research.dividend_history import collect

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--start',type=date.fromisoformat,default=date(2021,1,1))
    p.add_argument('--end',type=date.fromisoformat,default=date(2026,11,24))
    p.add_argument('--folder',type=Path,default=settings.data_dir/'research/dividend_backtest_2021')
    p.add_argument('--stage',choices=['collect','finalize','run','all'],default='all')
    args=p.parse_args()
    if args.stage in ('collect','all'):
        collect(args.folder,args.start,args.end,lambda *args:print(*args,flush=True))
    if args.stage=='finalize':
        from app.research.dividend_history import finalize
        finalize(args.folder,args.start,args.end,lambda *args:print(*args,flush=True))
    if args.stage in ('run','all'):
        from app.research.dividend_report import build
        import json
        result=build(args.folder)
        print(json.dumps({'stats':result['stats'],'data_quality':result['data_quality']},ensure_ascii=False,default=str),flush=True)

if __name__=='__main__':main()
