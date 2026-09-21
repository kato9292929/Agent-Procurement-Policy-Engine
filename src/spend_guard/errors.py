"""Exit codes and error types.

Semantic uncertainty and system failure never share an exit code: a `REVIEW`
caused by a low-confidence judgment is an ordinary outcome, while a provider
outage or a ledger write failure is an operational problem that should page
someone. Codes follow the SemDecide convention of keeping the two apart.
"""

from __future__ import annotations

EXIT_PAY = 0
EXIT_HOLD = 10
EXIT_REVIEW = 20
EXIT_INPUT_ERROR = 2
EXIT_PROVIDER_ERROR = 4
EXIT_LEDGER_ERROR = 5

DECISION_EXIT_CODES = {"PAY": EXIT_PAY, "HOLD": EXIT_HOLD, "REVIEW": EXIT_REVIEW}


class SpendGuardError(Exception):
    """Base class for errors Spend Guard raises on purpose."""


class InputError(SpendGuardError):
    """Local input is missing, malformed, or too large. Jev was not called."""


class ProviderError(SpendGuardError):
    """Jev was unreachable, refused the request, or returned unusable data.

    Provider-specific detail is kept in `detail` for operator logs and is
    deliberately never written to the ledger.
    """

    def __init__(self, message: str, *, kind: str = "PROVIDER_ERROR", detail: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.detail = detail


class LedgerError(SpendGuardError):
    """The ledger could not be read or appended to."""
