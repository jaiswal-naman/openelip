"""Evaluation harness: replay a gold set through the model gateway, record the run, gate on its score (PLT-08).

Usage is in evals/README.md. The gold sets, recordings and floors live in evals/ at the repository root.
"""

from pathlib import Path

EVALS = Path(__file__).resolve().parents[3] / "evals"

# Suites that only prove the harness works. Their runs say nothing about quality and are never shown as a result.
HARNESS_ONLY = frozenset({"harness_smoke"})
