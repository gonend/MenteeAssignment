from __future__ import annotations

from typing import Any, Dict, Iterable, Iterator, Sequence, Tuple


class SchemaProjector:
    """Wraps a row iterator; ensures every row contains all expected keys.

    Fills absent keys with None. Never overwrites real values. Never
    synthesizes numeric data — None is an explicit "no measurement yet"
    sentinel (per Claude_Plan.md §9: fabrication on gaps is forbidden;
    advertising absence is not fabrication).
    """

    def __init__(
        self,
        source: Iterable[Dict[str, Any]],
        expected_keys: Sequence[str],
    ) -> None:
        self._source = source
        self._expected: Tuple[str, ...] = tuple(expected_keys)

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        expected = self._expected
        for row in self._source:
            out = dict(row)
            for k in expected:
                if k not in out:
                    out[k] = None
            yield out
