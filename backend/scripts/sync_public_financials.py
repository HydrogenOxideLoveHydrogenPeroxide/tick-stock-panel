"""Manual public financial refresh, using the same adapters as the web UI."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import settings
from app.services.public_financials import sync_table, REPORTS
from app.services.dividend_screen import sync_snapshot

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--table',choices=[*REPORTS,'dividend_screen','all'],default='all')
    parser.add_argument('--data-dir',type=Path,default=settings.data_dir)
    args=parser.parse_args()
    for table in REPORTS if args.table=='all' else [args.table]:
        if table!='dividend_screen':print(table,sync_table(args.data_dir,table),flush=True)
    if args.table in ('all','dividend_screen'):print('dividend_screen',sync_snapshot(args.data_dir),flush=True)

if __name__=='__main__':main()
