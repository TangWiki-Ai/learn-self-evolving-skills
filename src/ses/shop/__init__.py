"""Deterministic shop environment for the pinned STATE-Bench return case."""

from ses.shop.environment import (
    CASE_DEFINITION,
    CASE_ID,
    POLICY_VERSION,
    TOOL_SCHEMA_VERSION,
    CaseEnvironment,
    ShopRole,
    state_diff,
)
from ses.shop.fixture import PINNED_CASE_FIXTURE, ReturnCaseFixture, load_case_fixture
from ses.shop.policy import ReturnPolicyDecision, ReturnReason, compute_return_policy

__all__ = [
    "CASE_DEFINITION",
    "CASE_ID",
    "PINNED_CASE_FIXTURE",
    "POLICY_VERSION",
    "TOOL_SCHEMA_VERSION",
    "CaseEnvironment",
    "ReturnCaseFixture",
    "ReturnPolicyDecision",
    "ReturnReason",
    "ShopRole",
    "compute_return_policy",
    "load_case_fixture",
    "state_diff",
]
