"""Focused test: scripts/seed_demo.py works without PYTHONPATH."""
from __future__ import annotations

import os
import subprocess
import sys


def test_seed_demo_direct_invocation_without_pythonpath():
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    r = subprocess.run(
        [sys.executable, "scripts/seed_demo.py", "--confirm"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert r.returncode == 0, f"direct invocation failed: {r.stderr}\n{r.stdout}"
    assert "ReplayFingerprint" in r.stdout
    assert "seed=42" in r.stdout

    # --help also works without PYTHONPATH
    r2 = subprocess.run(
        [sys.executable, "scripts/seed_demo.py", "--help"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert r2.returncode == 0
    assert "--confirm" in r2.stdout
