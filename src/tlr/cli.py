"""Command line: ``python -m tlr all`` reproduces every result in the repository."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import metrics
from .config import SimConfig
from .pipeline import run_simulation
from .report import check_readme, run_all


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tlr", description=__doc__)
    sub = p.add_subparsers(dest="cmd")
    a = sub.add_parser("all", help="run every scenario, regenerate results/, figures and docs")
    a.add_argument("--root", default=".", type=Path)
    r = sub.add_parser("run", help="run one scenario and print its scorecard")
    r.add_argument("--no-recon", action="store_true", help="disable reconciliation and the ops desk")
    r.add_argument("--stress", action="store_true", help="3x incidents, allowed to overlap")
    r.add_argument("--orders-per-day", type=int, default=320)
    r.add_argument("--days", type=int, default=10)
    r.add_argument("--seed", type=int, default=SimConfig.seed)
    r.add_argument("--db", default=None, help="DuckDB file to write (default: in-memory)")
    c = sub.add_parser("check-docs", help="verify README results block matches results/results.json")
    c.add_argument("--root", default=".", type=Path)
    args = p.parse_args(argv)

    if args.cmd in (None, "all"):
        run_all(getattr(args, "root", Path(".")))
        return 0
    if args.cmd == "check-docs":
        ok = check_readme(args.root)
        print("README results block is up to date" if ok else "README results block is STALE: run `python -m tlr all`")
        return 0 if ok else 1
    kw = dict(recon_enabled=not args.no_recon, orders_per_day=args.orders_per_day, trading_days=args.days,
              seed=args.seed)
    cfg = SimConfig.stress(**kw) if args.stress else SimConfig(**kw)
    res = run_simulation(cfg, args.db)
    sc = metrics.score(res)
    out = {k: v for k, v in sc.items() if k not in ("incident_rows", "exception_labels")}
    out["volumes"] = metrics.volumes(res)
    out["end_of_window"] = metrics.book_differences(res)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
