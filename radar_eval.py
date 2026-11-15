# -*- coding: utf-8 -*-
"""
radar_eval.py — 雷達「題材群聚」「多來源」規則的預先登記評估(2026-11-15)
============================================================
方法與判定見 docs/prereg_radar_theme_cluster.md(登記後不得依結果修改)。
讀 verification_ledger.json → 寫 radar_eval.json。只描述,不改雷達收錄規則。
"""
import json
import random
import sys

LEDGER = "verification_ledger.json"
OUT = "radar_eval.json"
MIN_DATES = 30
N_BOOT = 2000
SEED = 20261115
RULES = {"theme_cluster": ("theme_cluster", "dual"), "source_diversity": ("source_diversity", "dual")}
BASE = "keyword_surge"


def day_means(ledger, types):
    by = {}
    for c in ledger:
        if c.get("detection_type") in types and c.get("excess_pct") is not None:
            by.setdefault(c["flagged_date"], []).append(c["excess_pct"])
    return {d: sum(v) / len(v) for d, v in by.items()}


def boot_ci(xs, n=N_BOOT, seed=SEED):
    rnd = random.Random(seed)
    ms = sorted(sum(rnd.choice(xs) for _ in xs) / len(xs) for _ in range(n))
    return ms[int(0.025 * n)], ms[int(0.975 * n) - 1]


def evaluate(ledger):
    base = day_means(ledger, (BASE,))
    out = {}
    for name, types in RULES.items():
        a = day_means(ledger, types)
        days = sorted(set(a) & set(base))
        diffs = [a[d] - base[d] for d in days]
        r = {"paired_dates": len(days), "min_dates": MIN_DATES}
        if diffs:
            r["mean_diff_pp"] = round(sum(diffs) / len(diffs), 2)
        if len(days) < MIN_DATES:
            r["status"] = "樣本不足"
        else:
            lo, hi = boot_ci(diffs)
            r["ci95_pp"] = [round(lo, 2), round(hi, 2)]
            r["status"] = "較好" if lo > 0 else "較差" if hi < 0 else "沒有差別的證據"
        out[name] = r
    return out


def main():
    try:
        with open(LEDGER, encoding="utf-8") as f:
            ledger = json.load(f)
    except (OSError, ValueError):
        print("讀不到 " + LEDGER)
        return 0
    res = {"prereg": "docs/prereg_radar_theme_cluster.md", "base": BASE,
           "metric": "配對標記日的平均超額報酬差(規則 − 只有關鍵字暴增,百分點),以標記日 bootstrap 95% 信賴區間",
           "rules": evaluate(ledger)}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
