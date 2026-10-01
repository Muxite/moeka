"""Usage, spend and budget for a host's own calls: ``kernel.usage``.

Everything a consumer needs to show usage without a ledger of its own::

    from moeka import Kernel
    from moeka.usage import Attribution

    kernel = Kernel(env, consumer="awork", budget=CapBudget(limit_usd=5))
    done = kernel.llm.generate_sync(msgs, GenerateOptions(
        attribution=Attribution(role="extractor", purpose="extract_claims")))
    kernel.usage.total(consumer="awork")             # tokens, cost, waste, cache savings
    kernel.usage.totals(["agent"], consumer="awork")  # per agent
    kernel.usage.budget()                             # limit, spent, reserved, remaining
    sub = kernel.usage.subscribe(print)               # live, never blocks a call

Documents follow ``schemas/usage-record.v1`` and ``schemas/budget-event.v1``
(``SCHEMA_VERSION``; ``kernel.usage.schema_versions()``).
"""

from nanobot.kernel.ledger import (
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    UNATTRIBUTED,
    Attribution,
    bind_attribution,
    current_attribution,
)
from nanobot.kernel.usage import (
    USAGE_EVENTS,
    WASTE_LABELS,
    Subscription,
    UsageFilter,
    UsageTotals,
    UsageView,
)

__all__ = [
    "SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "UNATTRIBUTED",
    "USAGE_EVENTS",
    "WASTE_LABELS",
    "Attribution",
    "Subscription",
    "UsageFilter",
    "UsageTotals",
    "UsageView",
    "bind_attribution",
    "current_attribution",
]
