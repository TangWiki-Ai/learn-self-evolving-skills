from __future__ import annotations

from ses.runner.fake import (
    ExecutableDevelopCase,
    develop_catalog_sha256,
    load_develop_catalog,
)


def test_catalog_hash_covers_the_actual_executable_fixture() -> None:
    catalog = load_develop_catalog()
    case_id, case = next(iter(catalog.items()))
    changed_fixture = case.fixture.model_copy(
        update={"user_prompt": case.fixture.user_prompt + " Please hurry."}
    )
    changed = {case_id: ExecutableDevelopCase(changed_fixture, case.expected_actions)}

    assert develop_catalog_sha256(changed) != develop_catalog_sha256(catalog)
