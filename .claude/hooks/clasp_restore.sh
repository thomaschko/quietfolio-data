#!/bin/bash
# SessionStart:還原 clasp 登入並拉下 Quietfolio GAS 專案(2026-11-23)
# 雲端環境的 setup script 讀不到環境變數,所以改在 session 開始時做。
# CLASPRC_B64 可為 base64 或原始 JSON;原始 JSON 結尾少的右大括號會自動補上。不印出憑證內容。
[ -z "$CLASPRC_B64" ] && exit 0
python3 - <<'PY' || exit 0
import base64, json, os, re
raw = os.environ["CLASPRC_B64"].strip()
cands = [raw]
try:
    cands.insert(0, base64.b64decode(re.sub(r"\s", "", raw) + "==", validate=False).decode("utf-8"))
except Exception:
    pass
for s in cands:
    for extra in ("", "}", "}}", "}}}"):
        try:
            d = json.loads(s + extra)
        except Exception:
            continue
        if isinstance(d, dict) and d.get("tokens"):
            p = os.path.expanduser("~/.clasprc.json")
            open(p, "w").write(json.dumps(d))
            os.chmod(p, 0o600)
            raise SystemExit(0)
raise SystemExit(1)
PY
SID="${QUIETFOLIO_SCRIPT_ID:-1_gCzIKvxFrLvggTNKdwnkjQ1A-MWJ3-CgxcGrb-FQgR1QaWsKmKfq6yo}"
GAS="${CLAUDE_PROJECT_DIR:-/home/user/quietfolio-data}/../quietfolio-gas"
if command -v clasp >/dev/null && [ ! -f "$GAS/.clasp.json" ]; then
  mkdir -p "$GAS" && (cd "$GAS" && timeout 120 clasp clone "$SID" >/dev/null 2>&1) || true
fi
exit 0
