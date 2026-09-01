"""SSH health probe — is a node actually usable by *us*?

Conductor's scraped data tells you whether **Conductor** can reach a node. It does not
tell you whether you can log in, whether ROCm is installed, or whether Docker runs. A
node counts as healthy only when, over our own key:

  1. it accepts a key-only login (no password prompt, no dropped session),
  2. `rocm-smi` is installed and reports at least `min_gpus` GPUs,
  3. `docker` is installed and `docker ps` succeeds.

Read-only: the probe runs three shell lookups and never touches the node's state.
"""
from __future__ import annotations

import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LOG = logging.getLogger("conductor_reserve.probe")

# Runs on the node via `bash -s` (fed on stdin, so nothing needs shell-quoting here).
REMOTE_SCRIPT = r"""
echo PROBE_BEGIN
if command -v rocm-smi >/dev/null 2>&1; then
  n=$(rocm-smi --showid --csv 2>/dev/null | grep -Ec '^card[0-9]+')
  if [ "$n" = "0" ]; then n=$(rocm-smi 2>/dev/null | grep -Ec '^[0-9]+ '); fi
  echo "ROCM=$n"
else
  echo "ROCM=missing"
fi
if command -v docker >/dev/null 2>&1; then
  if docker ps >/dev/null 2>&1; then echo "DOCKER=ok"; else echo "DOCKER=fail"; fi
else
  echo "DOCKER=missing"
fi
"""

# Key-only, non-interactive: a node that wants a password fails here by design.
SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "PreferredAuthentications=publickey",
    "-o", "NumberOfPasswordPrompts=0",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
]


def _ssh_reason(stderr: str) -> str:
    """Turn ssh's stderr into a short, stable reason string."""
    err = (stderr or "").strip()
    low = err.lower()
    if "permission denied" in low:
        return "ssh: permission denied (key rejected / password required)"
    if "timed out" in low or "timeout" in low:
        return "ssh: connection timed out"
    if "connection closed" in low or "kex_exchange_identification" in low:
        return "ssh: connection closed by host"
    if "could not resolve" in low or "name or service not known" in low:
        return "ssh: hostname does not resolve"
    if "connection refused" in low:
        return "ssh: connection refused"
    first = next((ln for ln in err.splitlines() if ln.strip()), "")
    return "ssh: " + (first[:90] if first else "no response")


def probe_host(host: str, *, user: str, key: str | None = None, min_gpus: int = 2,
               timeout_s: int = 30, retries: int = 1) -> dict:
    """Probe one host. Returns {ok, reason, gpus, docker, ssh}. Never raises."""
    cmd = ["ssh"]
    if key:
        cmd += ["-i", str(Path(key).expanduser())]
    cmd += SSH_OPTS + ["-o", f"ConnectTimeout={max(5, timeout_s // 3)}",
                       f"{user}@{host}", "bash -s"]

    out = err = ""
    for attempt in range(retries + 1):
        try:
            p = subprocess.run(cmd, input=REMOTE_SCRIPT, capture_output=True,
                               text=True, timeout=timeout_s)
            out, err = p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            out, err = "", "connection timed out"
        except OSError as e:  # ssh binary missing, etc.
            return {"ok": False, "reason": f"ssh: {e}", "ssh": False,
                    "gpus": None, "docker": None}
        if "PROBE_BEGIN" in out:
            break
        # A rejected key is deterministic — don't waste a retry on it.
        if "permission denied" in (err or "").lower():
            break
        if attempt < retries:
            LOG.debug("probe retry for %s: %s", host, (err or "").strip()[:80])

    if "PROBE_BEGIN" not in out:
        return {"ok": False, "reason": _ssh_reason(err), "ssh": False,
                "gpus": None, "docker": None}

    fields = dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)
    rocm, docker = fields.get("ROCM", "missing"), fields.get("DOCKER", "missing")
    gpus = int(rocm) if rocm.isdigit() else None

    reasons = []
    if gpus is None:
        reasons.append("rocm-smi not installed")
    elif gpus == 0:
        reasons.append("rocm-smi shows 0 GPUs")
    elif gpus < min_gpus:
        reasons.append(f"rocm-smi shows {gpus} GPU(s) (< min_gpus={min_gpus})")
    if docker == "missing":
        reasons.append("docker not installed")
    elif docker != "ok":
        reasons.append("docker ps failed (daemon down or no access)")

    return {"ok": not reasons, "reason": "; ".join(reasons), "ssh": True,
            "gpus": gpus, "docker": docker}


def probe_hosts(hosts: list[str], *, user: str, key: str | None = None, min_gpus: int = 2,
                timeout_s: int = 30, workers: int = 16, progress=None) -> dict[str, dict]:
    """Probe many hosts in parallel. Returns {host: result}."""
    if not hosts:
        return {}
    done = 0

    def one(h: str):
        nonlocal done
        r = probe_host(h, user=user, key=key, min_gpus=min_gpus, timeout_s=timeout_s)
        done += 1
        if progress:
            progress(f"  probe {done}/{len(hosts)} {h}: "
                     f"{'OK' if r['ok'] else r['reason']}")
        return h, r

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(hosts)))) as ex:
        return dict(ex.map(one, hosts))


def probe_settings(config: dict) -> dict:
    """Read the `health_probe` block (with sensible fallbacks) from config."""
    hp = config.get("health_probe") or {}
    return {
        "enabled": bool(hp.get("enabled", True)),
        "user": hp.get("user") or config.get("ssh_user"),
        "key": hp.get("key"),
        "timeout_s": int(hp.get("timeout_s", 30)),
        "workers": int(hp.get("workers", 16)),
    }
