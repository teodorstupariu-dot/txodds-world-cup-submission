from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "outputs" / "test_collection.json"


class CollectionCounter:
    def __init__(self) -> None:
        self.count = 0

    def pytest_collection_modifyitems(self, session: pytest.Session, config: pytest.Config, items: list[pytest.Item]) -> None:
        self.count = len(items)


def main() -> int:
    counter = CollectionCounter()
    exit_code = int(pytest.main(["--collect-only", "-q", str(ROOT / "tests")], plugins=[counter]))
    payload = {"status": "PASS" if exit_code == 0 and counter.count > 0 else "FAIL", "tests_collected": counter.count, "pytest_exit_code": exit_code}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
