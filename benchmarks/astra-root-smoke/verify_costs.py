"""Verify published arithmetic offline; never invokes a model."""
import hashlib
import json
from decimal import Decimal
from pathlib import Path

root = Path(__file__).resolve().parent
data = json.loads((root / "results.json").read_text())
for name, digest in data["fixture_source"]["sha256"].items():
    assert hashlib.sha256((root / "fixture" / name).read_bytes()).hexdigest() == digest, name

for case in data["cases"]:
    total = Decimal(0)
    for row in case["usage"]:
        rates = data["pricing"]["rates"][row["model"]]
        for field in ("uncached_input", "cached_input", "cache_creation_input", "output"):
            assert isinstance(row[field], int) and row[field] >= 0
            total += Decimal(row[field]) * Decimal(rates[field]) / Decimal(1000000)
        if row["reasoning_subset"] is not None:
            assert 0 <= row["reasoning_subset"] <= row["output"]
    assert total == Decimal(case["standard_api_equivalent_usd"]), case["id"]
    assert case["usage_complete"] == (case["status"] == "completed")
    print(f"{case['id']}: ${total} ({case['status']})")
