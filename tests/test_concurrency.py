"""Cross-process appends (chapter 9).

Real deployments run more than one worker, so the interesting claim is that
`flock` serialises separate *processes*, not just threads in one interpreter.
This spawns real processes to check it.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import support
from spend_guard.ledger import Ledger, verify_chain

WRITER = """
import sys
sys.path.insert(0, {src!r})
from spend_guard.ledger import Ledger

ledger = Ledger(sys.argv[1])
worker = sys.argv[2]
for index in range({count}):
    ledger.append(
        "candidate_observed",
        decision_id=f"dec_{{worker}}_{{index}}",
        candidate_id=f"cand_{{worker}}_{{index}}",
        request_id=f"req_{{worker}}_{{index}}",
        payload={{"input_hash": f"sha256:{{worker}}{{index}}"}},
    )
"""


class CrossProcessTests(unittest.TestCase):
    def test_separate_processes_produce_one_unbroken_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            src = str(support.ROOT / "src")
            script = WRITER.format(src=src, count=20)
            processes = [
                subprocess.Popen([sys.executable, "-c", script, str(path), str(worker)])
                for worker in range(6)
            ]
            for process in processes:
                self.assertEqual(process.wait(timeout=60), 0)

            events = Ledger(path).read()
            self.assertEqual(len(events), 120, "an append was lost or interleaved mid-line")
            self.assertEqual(verify_chain(events), [], "the chain forked across processes")
            self.assertEqual(len({e["event_hash"] for e in events}), 120)
            self.assertEqual(len({e["decision_id"] for e in events}), 120)


if __name__ == "__main__":
    unittest.main()
