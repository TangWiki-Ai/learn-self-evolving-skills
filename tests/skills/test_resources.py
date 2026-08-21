from __future__ import annotations

from ses.foundation.config import ProviderId
from ses.skills.resources import load_demo_resources


def test_packaged_resources_include_every_configured_provider_lock() -> None:
    resources = load_demo_resources()

    assert resources.model_lock.provider is ProviderId.SILICONFLOW
    assert resources.chatanywhere_model_lock.provider is ProviderId.CHATANYWHERE
    assert resources.runtime_config.models_lock_for(ProviderId.SILICONFLOW) == (
        "models.lock.json"
    )
    assert resources.runtime_config.models_lock_for(ProviderId.CHATANYWHERE) == (
        "models.chatanywhere.lock.json"
    )
    assert len(resources.chatanywhere_model_lock_sha256) == 64
