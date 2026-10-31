"""Reproduce structural failures without retaining private model content."""

import asyncio
import json

import httpx
import pytest
from test_outfit_provider import candidate, evaluation, response

from wardrobe.errors import BusinessError
from wardrobe.outfit_provider import DeepSeekEvaluator, ModelCallQuota


@pytest.mark.parametrize(
    "value,shape",
    [
        (
            {"evaluations": [], "PRIVATE ROOT": "PRIVATE VALUE"},
            {
                "root_type": "object",
                "root_field_count": 2,
                "has_evaluations": True,
                "exact_root_fields": False,
                "evaluations_type": "array",
                "evaluation_count": 0,
            },
        ),
        (
            {"PRIVATE ROOT": "PRIVATE VALUE"},
            {
                "root_type": "object",
                "root_field_count": 1,
                "has_evaluations": False,
                "exact_root_fields": False,
            },
        ),
        (
            {"evaluations": {"PRIVATE": "TEXT"}},
            {
                "root_type": "object",
                "root_field_count": 1,
                "has_evaluations": True,
                "exact_root_fields": True,
                "evaluations_type": "object",
            },
        ),
        (
            {"evaluations": []},
            {
                "root_type": "object",
                "root_field_count": 1,
                "has_evaluations": True,
                "exact_root_fields": True,
                "evaluations_type": "array",
                "evaluation_count": 0,
            },
        ),
        (["PRIVATE VALUE"], {"root_type": "array"}),
        ("PRIVATE VALUE", {"root_type": "string"}),
        (None, {"root_type": "null"}),
        (True, {"root_type": "boolean"}),
        (0, {"root_type": "number"}),
        (
            {"evaluations": [evaluation()] * 13},
            {
                "root_type": "object",
                "root_field_count": 1,
                "has_evaluations": True,
                "exact_root_fields": True,
                "evaluations_type": "array",
                "evaluation_count": 13,
            },
        ),
    ],
)
def test_invalid_structure_stays_rejected_with_private_persisted_shape(
    tmp_path, value, shape
):
    key = tmp_path / "key.json"
    key.write_text(json.dumps({"api_key": "synthetic-private-key-abcdef"}))
    key.chmod(0o600)
    quota = ModelCallQuota(tmp_path / "ledger.sqlite", "unlimited", create=True)
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json=response(json.dumps(value)))

    provider = DeepSeekEvaluator(key, quota, transport=httpx.MockTransport(handler))
    with pytest.raises(BusinessError) as error:
        asyncio.run(
            provider.evaluate([candidate(n) for n in range(1, 25)], {}, "daily")
        )
    assert error.value.code == "INVALID_MODEL_OUTPUT"
    assert provider.last_receipt["validation_reason"] == "evaluation_shape"
    assert provider.last_receipt["output_shape"] == shape
    with quota.connect() as db:
        stored = json.loads(db.execute("SELECT receipt FROM attempts").fetchone()[0])
    assert stored == provider.last_receipt
    assert "PRIVATE" not in json.dumps(stored)
    assert len(sent) == quota.reserved_calls() == 1


@pytest.mark.parametrize("count", [1, 3, 24])
def test_dynamic_request_count_and_low_randomness_keep_strict_binding(tmp_path, count):
    key = tmp_path / "key.json"
    key.write_text(json.dumps({"api_key": "synthetic-private-key-abcdef"}))
    key.chmod(0o600)
    quota = ModelCallQuota(tmp_path / "ledger.sqlite", "unlimited", create=True)

    def handler(request):
        body = json.loads(request.content)
        assert body["temperature"] == 0
        assert body["response_format"] == {"type": "json_object"}
        assert body["model"] == "deepseek-flash"
        prompt = body["messages"][0]["content"]
        assert f"exactly {min(8, count)} evaluations" in prompt
        assert 'ONLY root key is "evaluations"' in prompt
        return httpx.Response(200, json=response())

    provider = DeepSeekEvaluator(key, quota, transport=httpx.MockTransport(handler))
    result = asyncio.run(
        provider.evaluate([candidate(n) for n in range(1, count + 1)], {}, "daily")
    )
    assert result[0]["candidate_id"] == "1"
    assert provider.last_receipt["output_shape"]["exact_root_fields"] is True


def test_empty_candidate_set_never_dispatches_or_spends_quota(tmp_path):
    key = tmp_path / "key.json"
    key.write_text(json.dumps({"api_key": "synthetic-private-key-abcdef"}))
    key.chmod(0o600)
    quota = ModelCallQuota(tmp_path / "ledger.sqlite", "unlimited", create=True)
    provider = DeepSeekEvaluator(
        key, quota, transport=httpx.MockTransport(lambda _: pytest.fail("no request"))
    )
    with pytest.raises(BusinessError) as error:
        asyncio.run(provider.evaluate([], {}, "daily"))
    assert error.value.code == "NO_MODEL_CANDIDATES"
    assert quota.reserved_calls() == 0
