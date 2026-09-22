#!/usr/bin/env python3
"""Build A/B candidates from the agent's spend-guard-endpoints.json.

Derived rather than hand-written so the candidates cannot drift away from
the config they claim to represent. Re-run after the config changes:

    python3 scripts/build_aa_candidates.py \\
        --config ../x402-Autonomous-Agent-/config/spend-guard-endpoints.json
"""

import json, sys, hashlib
from pathlib import Path
sys.path.insert(0, "src")
import argparse
_p = argparse.ArgumentParser()
_p.add_argument("--config", default="../x402-Autonomous-Agent-/config/spend-guard-endpoints.json")
_p.add_argument("--out", default="fixtures/candidates/aa-routes.jsonl")
_a = _p.parse_args()
cfg = json.loads(Path(_a.config).expanduser().read_text(encoding="utf-8"))
modes = cfg["modes"]
out = []
for r in cfg["routes"]:
    if not r["in_ab_comparison"]:
        continue
    task = r["task"]
    params = json.dumps({"url": r["url"], "method": r["http_method"]}, sort_keys=True)
    required = list(task["required_data"])
    out.append({
      "candidate_id": f"aa_{r['route_key'].replace('-', '_')}",
      "request_id": f"req_aa_{r['route_key']}",
      "idempotency_key": f"idem_aa_{r['route_key']}",
      "observed_at": "2026-09-22T00:00:00Z",
      "task": {"purpose": task["purpose"], "required_data": required},
      "service": {
        "provider_id": r["provider_id"], "service_id": r["service_id"], "route_id": r["route_id"],
        "access_method": r["access_method"], "description": r["description"],
        "request_params_hash": "sha256:" + hashlib.sha256(params.encode()).hexdigest(),
      },
      "quote": {"amount": r["price"]["amount"], "currency": r["price"]["currency"],
                "network": r["price"]["network"]},
      "prior_acquisitions": [],
      "_description_source": r["description_source"]["kind"],
      "_consuming_mode": r["consuming_task"]["mode"],
      "_task_origin": task["task_origin"],
      # Grouped by where the task definition came from, not by whether one
      # exists: every route has one now, but only Mode A's are the agent's own.
      "_ab_group": "code_task" if task["task_origin"] == "code" else "demo_task",
    })
out.sort(key=lambda c: (c["_ab_group"] != "code_task", c["candidate_id"]))
path = Path(_a.out)
path.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in out), encoding="utf-8")
from spend_guard.normalize import normalize
print(f"{'candidate':32}{'group':12}{'desc':11}{'mode':5}{'price':>6}  req  missing")
for c in out:
    n = normalize(c)
    miss = [m for m in n.missing_fields if not m.startswith("prior_")]
    print(f"{c['candidate_id']:32}{c['_ab_group']:12}{c['_description_source']:11}"
          f"{c['_consuming_mode']:5}{c['quote']['amount']:>6}  {len(c['task']['required_data']):>2}   {miss or 'none'}")
g = {}
for c in out: g[c["_ab_group"]] = g.get(c["_ab_group"], 0) + 1
print(f"\n{len(out)} candidates, groups={g}, calls={len(out)*2}")
