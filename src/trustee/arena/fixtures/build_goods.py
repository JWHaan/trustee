"""Build the demo goods deterministically.

Deterministic matters: the manifest pins a sha256, so a zip rebuilt with the
current clock in its headers would produce a different hash on every machine
and the honest run would fail its own acceptance criterion. Fixed timestamps
+ fixed order = the same bytes everywhere.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
GOODS = ROOT / "goods"

HONEST = '''"""lambda_math — tiny functional helpers, no dependencies."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from functools import reduce
from typing import Any, TypeVar

T = TypeVar("T")


def compose(*fns: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """Right-to-left composition: compose(f, g)(x) == f(g(x))."""

    def composed(value: Any) -> Any:
        for fn in reversed(fns):
            value = fn(value)
        return value

    return composed


def pipe(value: Any, *fns: Callable[[Any], Any]) -> Any:
    """Left-to-right application: pipe(x, f, g) == g(f(x))."""
    return reduce(lambda acc, fn: fn(acc), fns, value)


def curry(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Unary currying for fixed-arity functions."""
    arity = fn.__code__.co_argcount

    def collect(*args: Any) -> Any:
        if len(args) >= arity:
            return fn(*args[:arity])
        return lambda *more: collect(*args, *more)

    return collect


def total(xs: Iterable[float]) -> float:
    return sum(xs)
'''

JUNK = '''"""lambda_math — tiny functional helpers, no dependencies."""

from __future__ import annotations

from typing import Any, Callable


def compose(*fns: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """Right-to-left composition. (Shipped broken: ignores every fn but the first.)"""
    first = fns[0] if fns else (lambda x: x)
    return lambda value: first(value)
'''

FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def build_zip(path: Path, files: dict[str, str]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, body in files.items():
            info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, body)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    honest = build_zip(GOODS / "lambda-module.zip", {"lambda_math.py": HONEST})
    build_zip(GOODS / "lambda-module-junk.zip", {"lambda_math.py": JUNK})
    manifest = {"lambda-module.zip": honest}
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"lambda-module.zip       sha256={honest}")
    print("lambda-module-junk.zip  (corrupt twin, deliberately not in the manifest)")


if __name__ == "__main__":
    main()
