"""USD per million tokens (first-party API list prices, Oct 2026). Versioned in code
so cost numbers in benchmarks are reproducible; update deliberately."""

from decimal import Decimal

# model -> (input, cached_input, output)
PRICES: dict[str, tuple[Decimal, Decimal, Decimal]] = {
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.01"), Decimal("0.50")),
    "claude-sonnet-5-5": (Decimal("2.00"), Decimal("0.20"), Decimal("10.00")),
    "claude-opus-5-5": (Decimal("4.00"), Decimal("0.20"), Decimal("20.00")),
    "claude-haiku-4-5": (Decimal("1.00"), Decimal("0.10"), Decimal("5.00")),
    "gpt-5-mini": (Decimal("0.25"), Decimal("0.025"), Decimal("2.00")),
}
MTOK = Decimal(1_000_000)


def cost_usd(
    model: str, input_tokens: int, cached_input_tokens: int, output_tokens: int
) -> Decimal:
    """Unknown models cost 0 here but are flagged by `is_priced` so budgets fail closed."""
    if model not in PRICES:
        return Decimal(0)
    inp, cached, out = PRICES[model]
    uncached = max(input_tokens - cached_input_tokens, 0)
    return (uncached * inp + cached_input_tokens * cached + output_tokens * out) / MTOK


def is_priced(model: str) -> bool:
    return model in PRICES
