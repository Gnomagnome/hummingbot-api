#!/usr/bin/env python3
"""Run every existing protected-order test without installing a test runner."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


class _Marks:
    def __getattr__(self, _name: str):
        return lambda function: function


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    sys.modules.setdefault("pytest", SimpleNamespace(mark=_Marks()))

    # Import only the protected boundary, not services/__init__.py and its
    # unrelated runtime integrations. The real image supplies FastAPI; the
    # tiny fallback keeps this fake contract runnable by the standard library.
    services = ModuleType("services")
    services.__path__ = [str(root / "services")]
    sys.modules["services"] = services
    try:
        import fastapi  # noqa: F401
    except ModuleNotFoundError:
        fastapi_stub = ModuleType("fastapi")

        class HTTPException(Exception):
            def __init__(self, status_code: int, detail: str):
                super().__init__(detail)
                self.status_code = status_code
                self.detail = detail

        fastapi_stub.HTTPException = HTTPException
        sys.modules["fastapi"] = fastapi_stub

    test_path = root / "tests" / "test_hyperliquid_protected_orders.py"
    spec = importlib.util.spec_from_file_location("protected_contract_tests", test_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {test_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    tests = [
        value
        for name, value in sorted(vars(module).items())
        if name.startswith("test_") and callable(value)
    ]
    if not tests:
        raise RuntimeError("no protected-order tests discovered")
    for test in tests:
        if inspect.signature(test).parameters:
            raise RuntimeError(f"fake-only runner does not support fixtures: {test.__name__}")
        result = test()
        if inspect.isawaitable(result):
            asyncio.run(result)
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
