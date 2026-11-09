"""
run_cso.py - daily automator for KC calendar spread options (Task "0000.6.3) LSEG - CSO", 16:45).

1. Run Code/kc_cso_ingest_lseg.py (daily mode: OI>0 today + RICs already stored, 30-day window).
2. If it succeeded, git add/commit/push the two parquets so Streamlit Cloud picks them up.
Needs LSEG Workspace open (the ingest talks to the local Workspace proxy).
Everything is appended to Automator/run_log.txt.
"""
import datetime
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = Path(__file__).resolve().parent / "run_log.txt"
INGEST = ROOT / "Code" / "kc_cso_ingest_lseg.py"
DATA = ["Database/KC_cso.parquet", "Database/KC_cso_underlying.parquet"]


def log(msg: str):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT), **kw)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main() -> int:
    log("=== CSO run start ===")
    code, out = run([sys.executable, str(INGEST), "--days", "30"], env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    tail = "\n".join(l for l in out.splitlines() if " INFO " in l or "ERROR" in l or "WARNING" in l)[-2000:]
    log(f"ingest exit {code}\n{tail}")
    if code != 0:
        log("ingest FAILED - nothing pushed")
        return 1
    for cmd in (["git", "add"] + DATA,
                ["git", "commit", "-m", f"auto: KC CSO update (LSEG) {datetime.date.today()}"],
                ["git", "push"]):
        code, out = run(cmd)
        if code != 0:
            if "nothing to commit" in out:
                log("git: nothing new to commit")
                return 0
            log(f"git FAILED: {' '.join(cmd)}\n{out[-800:]}")
            return 1
    log("pushed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
