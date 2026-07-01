from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pocket_signal_bot.trade_store import TradeStore


def _load_sessions(data_path: Path) -> list[dict[str, Any]]:
    if not data_path.exists():
        return []
    store = TradeStore(str(data_path))
    return store.all_sessions()


def generate_charts(
    data_path: str | Path,
    out_dir: str | Path | None = None,
) -> list[Path]:
    """
    Write PNG charts to out_dir (default: data/charts next to data file).
    Returns list of created PNG paths.
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as e:
        raise RuntimeError("Install matplotlib: pip install matplotlib") from e

    data_path = Path(data_path)
    out = Path(out_dir) if out_dir else data_path.parent / "charts"
    out.mkdir(parents=True, exist_ok=True)

    sessions = _load_sessions(data_path)
    created: list[Path] = []

    if not sessions:
        return created

    latest = sessions[-1]
    trades = latest.get("trades") or []
    cfg = latest.get("config") or {}
    label = cfg.get("time_pair") or latest.get("session_id", "session")

    # ── 1) Latest session: cumulative PnL ───────────────────────────────────
    if trades:
        xs = [int(t.get("trade_num") or i + 1) for i, t in enumerate(trades)]
        ys = [float(t.get("cumulative_pnl") or 0.0) for t in trades]
        per_pnl = [float(t.get("pnl") or 0.0) for t in trades]

        fig, axes = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
        fig.suptitle(f"Session {label} — cumulative PnL", fontsize=13)

        axes[0].plot(xs, ys, color="#2ecc71", linewidth=2, marker="o", markersize=4)
        axes[0].axhline(0, color="#888", linewidth=0.8, linestyle="--")
        axes[0].set_ylabel("Cumulative PnL ($)")
        axes[0].grid(True, alpha=0.3)
        axes[0].fill_between(xs, ys, 0, alpha=0.15, color="#2ecc71")

        colors = ["#2ecc71" if p > 0 else "#e74c3c" if p < 0 else "#95a5a6" for p in per_pnl]
        axes[1].bar(xs, per_pnl, color=colors, width=0.7)
        axes[1].axhline(0, color="#888", linewidth=0.8)
        axes[1].set_xlabel("Trade #")
        axes[1].set_ylabel("Trade PnL ($)")
        axes[1].grid(True, alpha=0.3, axis="y")

        fig.tight_layout()
        pnl_path = out / "latest_pnl.png"
        fig.savefig(pnl_path, dpi=140, bbox_inches="tight")
        plt.close(fig)
        created.append(pnl_path)

        # ── 2) Rolling win rate ─────────────────────────────────────────────
        wr_x: list[int] = []
        wr_y: list[float] = []
        w = l = 0
        for t in trades:
            n = int(t.get("trade_num") or 0)
            p = float(t.get("pnl") or 0.0)
            if p > 0:
                w += 1
            elif p < 0:
                l += 1
            if w + l > 0:
                wr_x.append(n)
                wr_y.append(w / (w + l) * 100.0)

        if wr_x:
            fig2, ax2 = plt.subplots(figsize=(10, 4))
            ax2.plot(wr_x, wr_y, color="#3498db", linewidth=2, marker="o", markersize=4)
            ax2.axhline(50, color="#888", linewidth=0.8, linestyle="--", label="50%")
            ax2.set_ylim(0, 100)
            ax2.set_xlabel("Trade #")
            ax2.set_ylabel("Win rate (%)")
            ax2.set_title(f"Session {label} — win rate (wins ÷ wins+losses)")
            ax2.grid(True, alpha=0.3)
            ax2.legend(loc="lower right")
            fig2.tight_layout()
            wr_path = out / "latest_winrate.png"
            fig2.savefig(wr_path, dpi=140, bbox_inches="tight")
            plt.close(fig2)
            created.append(wr_path)

    # ── 3) Compare completed sessions by time_pair (A/B tests) ──────────────
    completed = [s for s in sessions if s.get("trades")]
    by_label: dict[str, dict[str, Any]] = {}
    for s in completed:
        c = s.get("config") or {}
        key = str(c.get("time_pair") or c.get("experiment_label") or s.get("session_id"))
        sm = s.get("summary") or {}
        if key not in by_label or len(s.get("trades") or []) >= len(by_label[key].get("trades") or []):
            by_label[key] = s

    if len(by_label) >= 1:
        labels = sorted(by_label.keys(), key=lambda k: (by_label[k].get("config") or {}).get("timeframe_sec", 0))
        pnls = [float((by_label[k].get("summary") or {}).get("total_pnl") or 0.0) for k in labels]
        wrs = [
            float((by_label[k].get("summary") or {}).get("win_rate_pct") or 0.0)
            for k in labels
        ]
        counts = [int((by_label[k].get("summary") or {}).get("trades") or 0) for k in labels]

        fig3, axes3 = plt.subplots(1, 3, figsize=(12, 4.5))
        fig3.suptitle("Compare candle/expiry setups (latest run per label)", fontsize=13)

        bar_colors = ["#2ecc71" if p >= 0 else "#e74c3c" for p in pnls]
        axes3[0].bar(labels, pnls, color=bar_colors)
        axes3[0].axhline(0, color="#888", linewidth=0.8)
        axes3[0].set_title("Total PnL ($)")
        axes3[0].tick_params(axis="x", rotation=25)

        axes3[1].bar(labels, wrs, color="#3498db")
        axes3[1].axhline(50, color="#888", linewidth=0.8, linestyle="--")
        axes3[1].set_ylim(0, 100)
        axes3[1].set_title("Win rate (%)")
        axes3[1].tick_params(axis="x", rotation=25)

        axes3[2].bar(labels, counts, color="#9b59b6")
        axes3[2].set_title("Trades")
        axes3[2].tick_params(axis="x", rotation=25)

        for ax in axes3:
            ax.grid(True, alpha=0.3, axis="y")

        fig3.tight_layout()
        cmp_path = out / "compare_setups.png"
        fig3.savefig(cmp_path, dpi=140, bbox_inches="tight")
        plt.close(fig3)
        created.append(cmp_path)

        # ── 4) Dashboard: all sessions PnL curves overlaid ─────────────────
        fig4, ax4 = plt.subplots(figsize=(10, 5))
        for s in completed:
            c = s.get("config") or {}
            name = str(c.get("time_pair") or s.get("session_id"))
            ts = s.get("trades") or []
            if not ts:
                continue
            ax4.plot(
                [int(t.get("trade_num") or i + 1) for i, t in enumerate(ts)],
                [float(t.get("cumulative_pnl") or 0.0) for t in ts],
                linewidth=1.8,
                marker="o",
                markersize=3,
                label=name,
            )
        ax4.axhline(0, color="#888", linewidth=0.8, linestyle="--")
        ax4.set_xlabel("Trade #")
        ax4.set_ylabel("Cumulative PnL ($)")
        ax4.set_title("All sessions — cumulative PnL")
        ax4.grid(True, alpha=0.3)
        ax4.legend(loc="best", fontsize=9)
        fig4.tight_layout()
        dash_path = out / "dashboard.png"
        fig4.savefig(dash_path, dpi=140, bbox_inches="tight")
        plt.close(fig4)
        created.append(dash_path)

    return created


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate trading PNG charts from stored session data.")
    parser.add_argument(
        "--data",
        default="data/trading_history.json",
        help="Path to trading_history.json",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output directory for PNGs (default: data/charts)",
    )
    args = parser.parse_args()
    paths = generate_charts(args.data, args.out)
    if not paths:
        print(f"No trade data at {args.data} — run the bot and place some trades first.")
        return
    print("Charts written:")
    for p in paths:
        print(f"  {p.resolve()}")


if __name__ == "__main__":
    main()
