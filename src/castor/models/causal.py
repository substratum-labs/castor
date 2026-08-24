"""Typed causal-memory values and deterministic graph query results."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field


class MemoryRef(BaseModel):
    kind: Literal["memory"] = "memory"
    memory_id: str


class ExternalSource(BaseModel):
    kind: Literal["external"] = "external"
    uri: str
    fetched_at: datetime
    digest: str
    content_type: str = "text/plain"


ProvenanceRef = Annotated[MemoryRef | ExternalSource, Field(discriminator="kind")]


class CascadeMode(StrEnum):
    FORBID = "forbid"
    WARN = "warn"
    CASCADE = "cascade"


class ProvenanceNode(BaseModel):
    ref: ProvenanceRef
    trust: float
    reason: str = ""
    truncated_content: str = ""


class DroppedEdge(BaseModel):
    kind: Literal["dropped"] = "dropped"
    reason: str
    original_target_id: str


class ProvenanceGraph(BaseModel):
    root: str
    direction: Literal["sources", "derivers", "both"]
    nodes: dict[str, ProvenanceNode]
    edges: list[tuple[str, str]]
    truncated_at_max_depth: bool
    dropped_edges: list[DroppedEdge] = Field(default_factory=list)
