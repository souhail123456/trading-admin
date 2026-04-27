"""
Validator Agent
---------------
Applies hard kill criteria to strategies that have been backtested and stress tested.

Kill criteria (ALL must pass or strategy dies):
  a) Beat buy-and-hold SPY in out-of-sample after costs
  b) OOS Sharpe > 0.7
  c) Survive parameter sensitivity (all variants Sharpe > 0)
  d) Max drawdown < 25%

Anti-overfit: if >20 parameter variants tested, auto-kill.

Usage:
    python -m pipeline.agents.validator --all
    python -m pipeline.agents.validator --strategy-id 5
"""

import argparse
import json
import logging
import sqlite3

from pipeline.db import init_db, log_agent_action

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# Hard kill thresholds
MIN_OOS_SHARPE = 0.7
MAX_DRAWDOWN = -25.0  # percent
MAX_PARAM_VARIANTS = 20


def validate_strategy(strategy: dict, conn: sqlite3.Connection) -> dict:
    """
    Apply all kill criteria to a strategy.
    Returns verdict with pass/fail for each criterion.
    """
    sid = strategy["id"]
    name = strategy["name"]
    log.info(f"\nValidating [{sid}] {name}")

    verdict = {
        "strategy_id": sid,
        "name": name,
        "criteria": {},
        "passed": True,
        "kill_reasons": [],
    }

    # Get OOS backtest
    oos = conn.execute(
        "SELECT * FROM backtest_runs WHERE strategy_id = ? AND run_type = 'out_of_sample' ORDER BY id DESC LIMIT 1",
        (sid,),
    ).fetchone()

    # Get stress test
    stress = conn.execute(
        "SELECT * FROM backtest_runs WHERE strategy_id = ? AND run_type = 'stress' ORDER BY id DESC LIMIT 1",
        (sid,),
    ).fetchone()

    if not oos:
        verdict["passed"] = False
        verdict["kill_reasons"].append("No out-of-sample backtest found")
        return verdict

    oos = dict(oos)
    stress_report = json.loads(dict(stress)["report"]) if stress else {}

    # -------------------------------------------------------------------
    # Criterion A: Beat SPY out-of-sample
    # -------------------------------------------------------------------
    beat_spy = oos.get("beat_spy")
    if beat_spy is None:
        # FX/no benchmark — skip this criterion
        verdict["criteria"]["beat_spy"] = {"status": "skipped", "reason": "no equity benchmark"}
    elif beat_spy:
        verdict["criteria"]["beat_spy"] = {"status": "PASS"}
        log.info(f"  [A] Beat SPY: PASS")
    else:
        verdict["criteria"]["beat_spy"] = {"status": "FAIL"}
        verdict["passed"] = False
        verdict["kill_reasons"].append("Does not beat buy-and-hold SPY out-of-sample")
        log.info(f"  [A] Beat SPY: FAIL")

    # -------------------------------------------------------------------
    # Criterion B: OOS Sharpe > 0.7
    # -------------------------------------------------------------------
    oos_sharpe = oos.get("sharpe")
    if oos_sharpe is not None and oos_sharpe > MIN_OOS_SHARPE:
        verdict["criteria"]["oos_sharpe"] = {"status": "PASS", "value": oos_sharpe, "threshold": MIN_OOS_SHARPE}
        log.info(f"  [B] OOS Sharpe {oos_sharpe} > {MIN_OOS_SHARPE}: PASS")
    elif oos_sharpe is not None:
        verdict["criteria"]["oos_sharpe"] = {"status": "FAIL", "value": oos_sharpe, "threshold": MIN_OOS_SHARPE}
        verdict["passed"] = False
        verdict["kill_reasons"].append(f"OOS Sharpe {oos_sharpe:.3f} < {MIN_OOS_SHARPE}")
        log.info(f"  [B] OOS Sharpe {oos_sharpe} > {MIN_OOS_SHARPE}: FAIL")
    else:
        verdict["criteria"]["oos_sharpe"] = {"status": "FAIL", "reason": "no Sharpe data"}
        verdict["passed"] = False
        verdict["kill_reasons"].append("No OOS Sharpe available")

    # -------------------------------------------------------------------
    # Criterion C: Parameter sensitivity — all variants positive Sharpe
    # -------------------------------------------------------------------
    stability = stress_report.get("sensitivity", {}).get("stability", {})
    if stability:
        all_positive = stability.get("all_positive", False)
        if all_positive:
            verdict["criteria"]["param_sensitivity"] = {
                "status": "PASS",
                "variants": stability.get("variant_count"),
                "sharpe_range": f"{stability.get('sharpe_min')} to {stability.get('sharpe_max')}",
            }
            log.info(f"  [C] Param sensitivity: PASS (all {stability.get('variant_count')} variants positive)")
        else:
            verdict["criteria"]["param_sensitivity"] = {
                "status": "FAIL",
                "sharpe_min": stability.get("sharpe_min"),
            }
            verdict["passed"] = False
            verdict["kill_reasons"].append(
                f"Parameter sensitivity: some variants have negative Sharpe (min={stability.get('sharpe_min')})"
            )
            log.info(f"  [C] Param sensitivity: FAIL (min Sharpe={stability.get('sharpe_min')})")
    else:
        verdict["criteria"]["param_sensitivity"] = {"status": "skipped", "reason": "no stress test data"}

    # -------------------------------------------------------------------
    # Criterion D: Max drawdown < 25%
    # -------------------------------------------------------------------
    max_dd = oos.get("max_drawdown")
    if max_dd is not None and max_dd > MAX_DRAWDOWN:
        verdict["criteria"]["max_drawdown"] = {"status": "PASS", "value": max_dd, "threshold": MAX_DRAWDOWN}
        log.info(f"  [D] Max DD {max_dd}% > {MAX_DRAWDOWN}%: PASS")
    elif max_dd is not None:
        verdict["criteria"]["max_drawdown"] = {"status": "FAIL", "value": max_dd, "threshold": MAX_DRAWDOWN}
        verdict["passed"] = False
        verdict["kill_reasons"].append(f"Max drawdown {max_dd:.1f}% exceeds {MAX_DRAWDOWN}% limit")
        log.info(f"  [D] Max DD {max_dd}% > {MAX_DRAWDOWN}%: FAIL")
    else:
        verdict["criteria"]["max_drawdown"] = {"status": "FAIL", "reason": "no drawdown data"}
        verdict["passed"] = False

    # -------------------------------------------------------------------
    # Anti-overfit check
    # -------------------------------------------------------------------
    total_variants = conn.execute(
        "SELECT MAX(variant_number) FROM backtest_runs WHERE strategy_id = ?",
        (sid,),
    ).fetchone()[0] or 0

    if total_variants > MAX_PARAM_VARIANTS:
        verdict["criteria"]["overfit_check"] = {"status": "FAIL", "variants": total_variants}
        verdict["passed"] = False
        verdict["kill_reasons"].append(f"Overfit: {total_variants} variants tested (limit: {MAX_PARAM_VARIANTS})")
    else:
        verdict["criteria"]["overfit_check"] = {"status": "PASS", "variants": total_variants}

    # -------------------------------------------------------------------
    # Monte Carlo sanity check (warning only, not a hard kill)
    # -------------------------------------------------------------------
    mc = stress_report.get("monte_carlo", {})
    mc_pct = mc.get("sharpe_percentile")
    if mc_pct is not None:
        if mc_pct >= 50:
            verdict["criteria"]["monte_carlo"] = {"status": "PASS", "percentile": mc_pct}
            log.info(f"  [MC] Percentile {mc_pct}th: PASS (edge likely real)")
        else:
            verdict["criteria"]["monte_carlo"] = {"status": "WARNING", "percentile": mc_pct}
            log.info(f"  [MC] Percentile {mc_pct}th: WARNING (may be luck)")

    return verdict


def apply_verdict(conn: sqlite3.Connection, verdict: dict) -> None:
    """Update strategy status and kill log based on verdict."""
    sid = verdict["strategy_id"]

    if verdict["passed"]:
        conn.execute(
            "UPDATE strategies SET status = 'backtest_pass' WHERE id = ?",
            (sid,),
        )
        log.info(f"  → STATUS: backtest_pass ✓")
    else:
        reason = "; ".join(verdict["kill_reasons"])
        conn.execute(
            "UPDATE strategies SET status = 'backtest_fail', killed_at = datetime('now'), kill_reason = ? WHERE id = ?",
            (reason, sid),
        )
        conn.execute(
            "INSERT INTO kill_log (strategy_id, phase, criterion, details) VALUES (?, 'backtest', ?, ?)",
            (sid, verdict["kill_reasons"][0], reason),
        )
        log.info(f"  → STATUS: backtest_fail ✗ ({reason})")

    conn.commit()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def validate_all(strategy_id: int | None = None, db_path: str | None = None) -> list[dict]:
    conn = init_db(db_path)
    verdicts = []

    if strategy_id:
        rows = conn.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM strategies WHERE status = 'candidate' AND id >= 5 ORDER BY id"
        ).fetchall()

    strategies = [dict(r) for r in rows]

    for strat in strategies:
        verdict = validate_strategy(strat, conn)
        apply_verdict(conn, verdict)

        log_agent_action(
            conn, "validator", "validation_completed",
            inputs={"strategy_id": strat["id"]},
            outputs=verdict,
            strategy_id=strat["id"],
        )
        verdicts.append(verdict)

    # Final report
    print(f"\n{'='*70}")
    print("VALIDATION RESULTS")
    print(f"{'='*70}")

    passed = [v for v in verdicts if v["passed"]]
    failed = [v for v in verdicts if not v["passed"]]

    if passed:
        print(f"\n✓ PASSED ({len(passed)}):")
        for v in passed:
            print(f"  [{v['strategy_id']}] {v['name']}")
            for crit, detail in v["criteria"].items():
                status = detail.get("status", "?")
                print(f"      {crit}: {status}")

    if failed:
        print(f"\n✗ FAILED ({len(failed)}):")
        for v in failed:
            print(f"  [{v['strategy_id']}] {v['name']}")
            for reason in v["kill_reasons"]:
                print(f"      ✗ {reason}")

    print(f"\nSurvivors ready for Phase 3 (paper trading): {len(passed)}")
    return verdicts


def main():
    parser = argparse.ArgumentParser(description="Validator — apply kill criteria")
    parser.add_argument("--strategy-id", type=int, default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--db", type=str, default=None)
    args = parser.parse_args()

    sid = args.strategy_id if not args.all else None
    validate_all(strategy_id=sid, db_path=args.db)


if __name__ == "__main__":
    main()
