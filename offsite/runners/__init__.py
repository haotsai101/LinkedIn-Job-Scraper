"""Model runners for the offsite agent (OA8 NIM, OA9 Claude).

Each runner drives guard-mcp with one model until the model ends its turn, a
control outcome is set (``RunControl.outcome`` — ready / human / loop / budget),
or something breaks. All runners return the same ``RunResult``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RunStatus = Literal["finished", "error"]


@dataclass
class RunResult:
    """How one model run ended, from the runner's side.

    ``finished`` — the model ended its turn or a control outcome stopped it
    (read ``RunControl.outcome`` for which). ``error`` — the model/API failed:
    timeout, HTTP error / 429, invalid tool call or output after one retry,
    turn limit; ``error`` says which (OA10 fallback trigger).
    """

    status: RunStatus
    model: str
    error: str | None = None
    turns: int = 0
    seconds: float = 0.0
    final_text: str = ""
