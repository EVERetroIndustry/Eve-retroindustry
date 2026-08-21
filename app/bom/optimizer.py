"""
Make vs. Buy optimizer.

Algorithm (bottom-up):
  For each node in the tree we compute the optimal cost:
  - leaf:     input_price × quantity
  - non-leaf: min(make_cost, buy_cost)
              make_cost = job install fee + sum of children's optimal costs
              buy_cost  = input_price × quantity

  make_cost includes the node's OWN job install fee (node.job_fee) so the
  decision is all-in: building a component only wins if its materials + fee
  beat buying it. Without the fee term the optimizer over-selects "make" and
  the (real) install fees then silently erase the paper savings.

  input_price is Jita sell (index 0) or Jita buy (index 1) depending on the
  `input_basis` argument - real builders source inputs from buy orders, which
  is cheaper than instant-buying at sell.

  The result is the minimum total cost and a list of decisions for each intermediate.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal
from app.bom.resolver import BOMNode


@dataclass
class Decision:
    type_id: int
    name: str
    quantity: int
    make_cost: float | None     # manufacturing cost (with optimal children)
    buy_cost: float | None      # Jita sell price × qty
    action: Literal["make", "buy", "unknown"]
    savings: float | None       # positive = cheaper to make, negative = cheaper to buy


@dataclass
class OptimizationResult:
    total_cost: float | None
    naive_cost: float | None            # without optimization (make everything)
    total_savings: float | None         # naive - optimized
    decisions: list[Decision] = field(default_factory=list)

    @property
    def buy_decisions(self) -> list[Decision]:
        return [d for d in self.decisions if d.action == "buy"]

    @property
    def make_decisions(self) -> list[Decision]:
        return [d for d in self.decisions if d.action == "make"]


def optimize(
    root: BOMNode,
    prices: dict[int, tuple[float | None, float | None]],
    input_basis: str = "sell",
) -> OptimizationResult:
    """
    Run make vs. buy optimization on the BOM tree.
    prices: {type_id: (sell_price, buy_price)}
    input_basis: "sell" (index 0) or "buy" (index 1) - which price to pay for inputs.
    """
    price_idx = 1 if input_basis == "buy" else 0
    raw_decisions: list[Decision] = []
    opt_cost   = _optimize_node(root, prices, raw_decisions, is_root=True, price_idx=price_idx)
    naive_cost = _naive_cost(root, prices, price_idx)

    # Deduplication: aggregate by type_id (same component in different branches)
    merged: dict[int, Decision] = {}
    for d in raw_decisions:
        if d.type_id in merged:
            ex = merged[d.type_id]
            new_qty  = ex.quantity  + d.quantity
            new_make = (ex.make_cost or 0) + (d.make_cost or 0) if (ex.make_cost is not None or d.make_cost is not None) else None
            new_buy  = (ex.buy_cost  or 0) + (d.buy_cost  or 0) if (ex.buy_cost  is not None or d.buy_cost  is not None) else None
            new_sav  = (new_make - new_buy) if (new_make is not None and new_buy is not None) else None
            new_act: Literal["make", "buy", "unknown"] = (
                "buy" if (new_buy is not None and new_make is not None and new_buy < new_make)
                else "make" if new_make is not None
                else "buy" if new_buy is not None
                else "unknown"
            )
            merged[d.type_id] = Decision(d.type_id, d.name, new_qty, new_make, new_buy, new_act, new_sav)
        else:
            merged[d.type_id] = d

    decisions = sorted(merged.values(), key=lambda d: -(abs(d.savings or 0)))
    savings = (naive_cost - opt_cost) if (naive_cost is not None and opt_cost is not None) else None

    return OptimizationResult(
        total_cost=opt_cost,
        naive_cost=naive_cost,
        total_savings=savings,
        decisions=decisions,
    )


def _optimize_node(
    node: BOMNode,
    prices: dict[int, tuple[float | None, float | None]],
    decisions: list[Decision],
    is_root: bool = False,
    price_idx: int = 0,
) -> float | None:
    price = prices.get(node.type_id, (None, None))[price_idx]

    if node.is_leaf:
        return (price * node.quantity) if price is not None else None

    # Recursively compute the optimal manufacturing cost (children already optimized)
    children_costs = [
        _optimize_node(child, prices, decisions, price_idx=price_idx)
        for child in node.children
    ]
    if any(c is None for c in children_costs):
        make_cost = None
    else:
        # All-in make cost = this job's install fee + optimal children.
        make_cost = sum(children_costs) + node.job_fee

    buy_cost = (price * node.quantity) if price is not None else None

    # Decision
    if make_cost is not None and buy_cost is not None:
        action: Literal["make", "buy", "unknown"] = "buy" if buy_cost < make_cost else "make"
        savings = make_cost - buy_cost   # positive = cheaper to make, negative = cheaper to buy
    elif buy_cost is None:
        action = "make"
        savings = None
    else:
        action = "buy"
        savings = None

    # The root (the product itself) is not added to decisions
    if not is_root:
        decisions.append(Decision(
            type_id=node.type_id,
            name=node.name,
            quantity=node.quantity,
            make_cost=make_cost,
            buy_cost=buy_cost,
            action=action,
            savings=savings,
        ))

    if action == "buy" and buy_cost is not None:
        return buy_cost
    return make_cost


def get_shopping_list(
    root: BOMNode,
    decisions: dict[int, Decision],
) -> dict[int, tuple[str, int]]:
    """
    Build a shopping list from the optimization results.
    For each node:
      - "buy" decision → add the component to the shopping list, don't descend further
      - leaf (raw material) → add to the shopping list
      - "make" → continue into the children
    Returns {type_id: (name, total_quantity)}.
    """
    shopping: dict[int, tuple[str, int]] = {}

    def _add(type_id: int, name: str, qty: int):
        prev_qty = shopping.get(type_id, (name, 0))[1]
        shopping[type_id] = (name, prev_qty + qty)

    def traverse(node: BOMNode, is_root: bool = False):
        if node.is_leaf:
            _add(node.type_id, node.name, node.quantity)
            return

        decision = decisions.get(node.type_id)
        if not is_root and decision and decision.action == "buy":
            _add(node.type_id, node.name, node.quantity)
            return  # don't descend further - we buy the finished component

        for child in node.children:
            traverse(child)

    traverse(root, is_root=True)
    return shopping


def _naive_cost(
    node: BOMNode,
    prices: dict[int, tuple[float | None, float | None]],
    price_idx: int = 0,
) -> float | None:
    """Naive cost - make everything, buy the leaves. Includes each job's install
    fee, so 'naive' is the true all-in cost of building the whole tree."""
    if node.is_leaf:
        price = prices.get(node.type_id, (None, None))[price_idx]
        return (price * node.quantity) if price is not None else None

    child_costs = [_naive_cost(c, prices, price_idx) for c in node.children]
    if any(c is None for c in child_costs):
        return None
    return sum(child_costs) + node.job_fee
