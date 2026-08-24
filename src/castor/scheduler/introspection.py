"""Bounded, stateless execution of structured journal queries."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from typing import Any

from castor.models.checkpoint import SyscallRecord
from castor.models.introspection import (
    FindDecisionsQuery,
    FindDecisionsResult,
    FindSyscallQuery,
    FindSyscallResult,
    GetReasoningChainQuery,
    GetSyscallQuery,
    GetSyscallResult,
    IntrospectionQuery,
    IntrospectionResult,
    IntrospectionTargetNotFoundError,
    PartialResult,
    ReasoningChainResult,
    SummarizeQuery,
    SummarizeResult,
    SyscallSnapshot,
)


class IntrospectionEngine:
    """Execute v1 journal queries without mutating their source records."""

    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self._clock = clock

    def execute(
        self,
        query: IntrospectionQuery,
        journal: list[SyscallRecord],
        deadline_ms: float = 100.0,
    ) -> IntrospectionResult:
        started = self._clock()
        deadline = started + deadline_ms / 1000
        try:
            if isinstance(query, FindSyscallQuery):
                payload = self._find_syscall(query, journal, deadline)
            elif isinstance(query, GetSyscallQuery):
                payload = self._get_syscall(query, journal)
            elif isinstance(query, GetReasoningChainQuery):
                payload = self._get_reasoning_chain(query, journal, deadline)
            elif isinstance(query, SummarizeQuery):
                payload = self._summarize(query, journal, deadline)
            elif isinstance(query, FindDecisionsQuery):
                payload = self._find_decisions(query, journal, deadline)
            else:  # pragma: no cover - protected by Pydantic query models
                raise TypeError(f"Unsupported introspection query: {type(query)!r}")
        except _TimeoutError as timeout:
            payload = PartialResult(
                partial_payload=timeout.partial_payload,
                timeout_at_step=timeout.step,
            )
        return IntrospectionResult(
            query_type=query.type,
            payload=payload,
            duration_ms=(self._clock() - started) * 1000,
        )

    def _find_syscall(self, q: FindSyscallQuery, journal, deadline):
        matches: list[SyscallSnapshot] = []
        exceeded_limit = False
        for index, item in self._bounded_records(journal, q.step_range, deadline):
            if q.purpose is not None and item.purpose != q.purpose:
                continue
            if q.syscall_name is not None and self._name(item) != q.syscall_name:
                continue
            if q.cost_min is not None and item.cost < q.cost_min:
                continue
            if q.duration_ms_min is not None and item.duration_ms < q.duration_ms_min:
                continue
            if len(matches) >= q.limit:
                exceeded_limit = True
                break
            matches.append(self._snapshot(index, item))
        return FindSyscallResult(matches=matches, truncated=exceeded_limit)

    def _get_syscall(self, q: GetSyscallQuery, journal):
        for index, item in enumerate(journal):
            if (isinstance(q.target, int) and index == q.target) or (
                isinstance(q.target, str) and item.invocation_id == q.target
            ):
                return GetSyscallResult(
                    snapshot=self._snapshot(index, item, q.include_full_output)
                )
        raise IntrospectionTargetNotFoundError(f"No syscall found for {q.target!r}")

    def _get_reasoning_chain(self, q, journal, deadline):
        if not 0 <= q.target_step < len(journal):
            raise IntrospectionTargetNotFoundError(
                f"No syscall at step {q.target_step}"
            )
        target = journal[q.target_step]
        target_args = str(target.request.get("arguments", {}))
        chain = [self._snapshot(q.target_step, target)]
        prior_matches: list[tuple[int, SyscallRecord]] = []
        for index in range(q.target_step - 1, -1, -1):
            self._check_deadline(deadline, index, {"chain": chain})
            item = journal[index]
            if (
                self._name(item) in {"llm", "llm_inference"}
                and str(item.response) in target_args
            ):
                prior_matches.append((index, item))
        # ``max_depth`` includes the target step itself, so a depth of one
        # returns the target only and never exposes an ancestor.
        ancestor_limit = max(0, q.max_depth - 1)
        selected = prior_matches[:ancestor_limit]
        for index, item in reversed(selected):
            chain.insert(0, self._snapshot(index, item))
        return ReasoningChainResult(
            target_step=q.target_step,
            chain=chain,
            truncated_at_max_depth=len(prior_matches) > ancestor_limit,
        )

    def _summarize(self, q, journal, deadline):
        records = list(self._bounded_records(journal, q.step_range, deadline))
        by_group: dict[str, dict[str, float]] | None = None
        if q.group_by != "none":
            by_group = {}
            for index, item in records:
                self._check_deadline(deadline, index, {"groups": by_group})
                key = (
                    item.purpose.value if q.group_by == "purpose" else self._name(item)
                )
                group = by_group.setdefault(
                    key, {"count": 0.0, "cost": 0.0, "duration_ms": 0.0, "errors": 0.0}
                )
                group["count"] += 1
                group["cost"] += item.cost
                group["duration_ms"] += item.duration_ms
                group["errors"] += float(self._is_error(item))
        return SummarizeResult(
            total_syscalls=len(records),
            total_cost=sum(item.cost for _, item in records),
            total_duration_ms=sum(item.duration_ms for _, item in records),
            error_count=sum(self._is_error(item) for _, item in records),
            by_group=by_group,
        )

    def _find_decisions(self, q, journal, deadline):
        try:
            pattern = re.compile(q.output_pattern)
        except re.error as error:
            raise ValueError(f"invalid output_pattern: {error}") from error
        matches: list[SyscallSnapshot] = []
        exceeded_limit = False
        for index, item in self._bounded_records(journal, q.step_range, deadline):
            if self._name(item) not in {"llm", "llm_inference"} or not pattern.search(
                str(item.response)
            ):
                continue
            if len(matches) >= q.limit:
                exceeded_limit = True
                break
            matches.append(self._snapshot(index, item))
        return FindDecisionsResult(matches=matches, truncated=exceeded_limit)

    def _bounded_records(self, journal, step_range, deadline):
        start, end = step_range if step_range is not None else (0, len(journal) - 1)
        for index in range(max(0, start), min(end, len(journal) - 1) + 1):
            self._check_deadline(deadline, index, {"processed": index})
            yield index, journal[index]

    def _check_deadline(self, deadline, index, partial_payload):
        if self._clock() > deadline:
            raise _TimeoutError(index, partial_payload)

    @staticmethod
    def _name(item: SyscallRecord) -> str:
        return str(item.request.get("tool_name", ""))

    @staticmethod
    def _is_error(item: SyscallRecord) -> bool:
        return item.raised_exception is not None or (
            isinstance(item.response, dict) and item.response.get("status") == "ERROR"
        )

    def _snapshot(self, index, item, include_full_output=False):
        args = str(item.request.get("arguments", {}))
        output = str(item.response)
        return SyscallSnapshot(
            invocation_id=item.invocation_id or f"journal-{index}",
            syscall_index=index,
            name=self._name(item),
            purpose=item.purpose,
            args_summary=self._summarize_text(args, 1024, include_full_output),
            output_summary=self._summarize_text(output, 4096, include_full_output),
            output_digest=hashlib.sha256(output.encode()).hexdigest(),
            cost=item.cost,
            duration_ms=item.duration_ms,
            timestamp=item.timestamp,
            raised_exception=item.raised_exception,
        )

    @staticmethod
    def _summarize_text(value: str, limit: int, include_full: bool) -> str:
        return value if include_full or len(value) <= limit else value[:limit] + "…"


class _TimeoutError(Exception):
    def __init__(self, step: int, partial_payload: Any) -> None:
        self.step = step
        self.partial_payload = partial_payload
