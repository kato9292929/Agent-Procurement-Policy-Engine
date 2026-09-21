"""x402 Spend Guard - shadow-mode purchase policy engine.

Shadow mode observes purchase candidates and records what it would have
decided. It never changes whether a payment happens.
"""

from .engine import ShadowEngine
from .hook import ShadowGuard
from .ledger import Ledger, LedgerIndex
from .models import Candidate, Decision, SCHEMA_VERSION
from .policy import Policy

__version__ = "0.1.0"

__all__ = [
    "ShadowEngine",
    "ShadowGuard",
    "Ledger",
    "LedgerIndex",
    "Candidate",
    "Decision",
    "Policy",
    "SCHEMA_VERSION",
    "__version__",
]
