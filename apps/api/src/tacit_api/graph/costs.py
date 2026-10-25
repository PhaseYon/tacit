"""Per-episode cost ledger + hard budget stop, carried in a contextvar so every LLM call
Graphiti makes (including its parallel sub-tasks) is attributed and checked."""

from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal

from ..llm import Usage


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class CostContext:
    workspace_id: str
    container_id: str | None
    graph_episode_id: str | None
    purpose: str
    budget_remaining_usd: Decimal  # computed once per episode from llm_usage
    records: list[tuple[str | None, Usage]] = field(default_factory=list)

    @property
    def spent(self) -> Decimal:
        return sum((u.cost_usd for _, u in self.records), Decimal(0))

    def check(self) -> None:
        if self.spent >= self.budget_remaining_usd:
            raise BudgetExceeded(
                f"LLM budget reached for workspace {self.workspace_id} "
                f"(remaining {self.budget_remaining_usd}, spent {self.spent})"
            )


current_cost: ContextVar[CostContext | None] = ContextVar("tacit_graph_cost", default=None)
current_prompt: ContextVar[str | None] = ContextVar("tacit_graph_prompt", default=None)
