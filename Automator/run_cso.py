"""
run_cso.py - daily automator for calendar spread options: KC, LRC, LCC (not scheduled yet).

1. Run Code/cso_ingest_lseg.py --commodity <K> for each commodity
   (daily mode: OI>0 today + RICs already stored, 30-day window, OI top-up).
2. git add/commit/push the parquets of the commodities that succeeded, so Streamlit Cloud picks them up.
Needs LSEG Workspace open (the ingest talks to the local Workspace proxy).
Everything is appended to Automator/run_log.txt.
"""
import datetime
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = Path(__file__).resolve().parent / "run_log.txt"
INGEST = ROOT / "Code" / "cso_ingest_lseg.py"
COMMODITIES = ["KC", "LRC", "LCC"]


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
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    data, failed = [], []
    for k in COMMODITIES:
        code, out = run([sys.executable, str(INGEST), "--commodity", k, "--days", "30"], env=env)
        tail = "\n".join(l for l in out.splitlines() if " INFO " in l or "ERROR" in l or "WARNING" in l)[-1500:]
        log(f"{k} ingest exit {code}\n{tail}")
        if code == 0:
            data += [f"Database/{k}_cso.parquet", f"Database/{k}_cso_underlying.parquet"]
        else:
            failed.append(k)
    data = [f for f in data if (ROOT / f).exists()]
    if not data:
        log("every ingest FAILED - nothing pushed")
        return 1
    msg = f"auto: CSO update (LSEG) {datetime.date.today()}" + (f" - failed: {failed}" if failed else "")
    for cmd in (["git", "add"] + data, ["git", "commit", "-m", msg], ["git", "push"]):
        code, out = run(cmd)
        if code != 0:
            if "nothing to commit" in out:
                log("git: nothing new to commit")
                return 1 if failed else 0
            log(f"git FAILED: {' '.join(cmd)}\n{out[-800:]}")
            return 1
    log("pushed" + (f" (failed: {failed})" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
