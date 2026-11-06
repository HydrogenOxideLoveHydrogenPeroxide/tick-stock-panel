"""Collect explicit public-source stock snapshots; no model calls or orders."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import settings
from app.services.fundamental_sync import collect
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--symbols',required=True,help='Comma-separated A-share symbols, at most20 per resumable batch')
    args=p.parse_args()
    result=collect(settings.data_dir,[s.strip() for s in args.symbols.split(',')])
    print(json.dumps({k:result[k] for k in ('status','total','completed','results','message')},ensure_ascii=False,indent=2))
