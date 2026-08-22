"""Trace construction and deterministic Judges."""

from ses.evaluation.aggregate import aggregate_case_grade, aggregate_status
from ses.evaluation.errors import (
    EvaluationError,
    EvaluationErrorCode,
    TraceBuildError,
)
from ses.evaluation.evidence import (
    escape_json_pointer_token,
    evidence_ref,
    join_json_pointer,
    state_diff_evidence,
    timeline_evidence,
    trace_event_evidence,
)
from ses.evaluation.judges import (
    Rule,
    RuleKind,
    forbidden_call,
    judge_rules,
    judge_rules_across_traces,
    judge_state,
    tool_arguments,
    tool_called,
    tool_count,
    tool_order,
)
from ses.evaluation.trace import (
    TraceMessage,
    TraceToolCall,
    build_trace,
    trace_messages,
    trace_tool_calls,
)

__all__ = [
    "EvaluationError",
    "EvaluationErrorCode",
    "Rule",
    "RuleKind",
    "TraceBuildError",
    "TraceMessage",
    "TraceToolCall",
    "aggregate_case_grade",
    "aggregate_status",
    "build_trace",
    "escape_json_pointer_token",
    "evidence_ref",
    "forbidden_call",
    "join_json_pointer",
    "judge_rules",
    "judge_rules_across_traces",
    "judge_state",
    "state_diff_evidence",
    "timeline_evidence",
    "tool_arguments",
    "tool_called",
    "tool_count",
    "tool_order",
    "trace_event_evidence",
    "trace_messages",
    "trace_tool_calls",
]
