"""Structured data the offsite agent hands back (design §6)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AnswerSource = Literal["profile", "job", "generated", "human"]


class GeneratedAnswer(BaseModel):
    """One filled form field, as reported by the agent in ``report_ready``."""

    model_config = ConfigDict(extra="forbid")

    field_label: str = Field(min_length=1, description="The field's label as shown on the form")
    answer: str = Field(description="What was entered/selected ('' if intentionally left blank)")
    source: AnswerSource = Field(
        description="profile = from the applicant profile/resume; job = from the job posting; "
                    "generated = written by you; human = done by the human")
    confidence: float = Field(ge=0.0, le=1.0, description="0..1 — how sure you are it is right")
    evidence: list[str] = Field(
        default_factory=list,
        description="Profile keys / resume lines / posting text the answer is based on")
    sensitive: bool = Field(
        description="True for sponsorship, work authorization, EEO/demographics, salary, "
                    "relocation, travel, background checks and legal attestations")
