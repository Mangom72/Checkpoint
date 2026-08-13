"""Keep repository names out of log output.

Actions logs on a public repository are world-readable, and the progress log
would otherwise reveal the names of private repositories. This filter rewrites
every log record so known repository names become stable aliases (``repo#03``).
The real names still go into ``manifest.json``, which only ever reaches the
storage backend - never the log.
"""

from __future__ import annotations

import logging
import re
import threading


class RepoNameRedactor(logging.Filter):
    def __init__(self, enabled: bool = False) -> None:
        super().__init__()
        self.enabled = enabled
        self._alias: dict[str, str] = {}   # full_name -> alias
        self._variants: dict[str, str] = {}  # any spelling -> alias
        self._pattern: re.Pattern[str] | None = None
        self._lock = threading.Lock()

    # -- registration ---------------------------------------------------
    def register(self, full_names: list[str]) -> None:
        """Assign an alias to each repository and rebuild the match pattern."""
        if not self.enabled:
            return
        from .util import safe_name

        with self._lock:
            for full_name in full_names:
                if full_name in self._alias:
                    continue
                alias = f"repo#{len(self._alias) + 1:02d}"
                self._alias[full_name] = alias
                name = full_name.split("/", 1)[-1]
                # The same repository shows up as owner/name, as the archive
                # slug and as the bare name, so all three must be covered.
                for variant in (full_name, safe_name(full_name), name):
                    if len(variant) >= 3:
                        self._variants[variant] = alias
            self._rebuild()

    def _rebuild(self) -> None:
        if not self._variants:
            self._pattern = None
            return
        # Longest first so "owner/name" wins over the bare "name".
        ordered = sorted(self._variants, key=len, reverse=True)
        self._pattern = re.compile("|".join(re.escape(v) for v in ordered))

    @property
    def aliases(self) -> dict[str, str]:
        with self._lock:
            return dict(self._alias)

    # -- logging.Filter -------------------------------------------------
    def filter(self, record: logging.LogRecord) -> bool:
        if not self.enabled or self._pattern is None:
            return True
        message = record.getMessage()
        scrubbed = self._pattern.sub(lambda m: self._variants[m.group(0)], message)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()
        return True

    def scrub(self, text: str) -> str:
        """Redact a plain string (for output that does not go through logging)."""
        if not self.enabled or self._pattern is None:
            return text
        return self._pattern.sub(lambda m: self._variants[m.group(0)], text)
