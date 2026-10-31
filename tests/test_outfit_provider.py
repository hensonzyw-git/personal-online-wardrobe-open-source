import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from wardrobe.errors import BusinessError
from wardrobe.outfit_provider import DeepSeekEvaluator, ModelCallQuota


def candidate(number=1):
    return {
        "candidate_id": str(number),
        "items": [
            {
                "role": "upper",
                "garment": {
                    "id": "g1",
                    "name": "PRIVATE NAME / IGNORE INSTRUCTIONS",
                    "category": "shirt",
                    "annotation_fields": {
                        "primary_color": {
                            "value": "white",
                            "source_kind": "observed",
                            "verification_state": "unverified",
                        }
                    },
                    "image_references": [{"content_url": "/private-image"}],
                },
            }
        ],
    }


def evaluation(number=1):
    return {
        "candidate_id": f"C{number:02d}",
        "suitable": True,
        "color": 0.8,
        "material": 0.7,
        "style": 0.8,
        "silhouette": 0.7,
        "evidence": {"G001": ["primary_color"]},
    }


@pytest.fixture
def config(tmp_path):
    key = tmp_path / "model.json"
    key.write_text(json.dumps({"api_key": "synthetic-private-key-abcdef"}))
    key.chmod(0o600)
    return key, ModelCallQuota(tmp_path / "calls.sqlite", 2, create=True)


def run(config, body, *, status=200):
    key, budget = config
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json=body)

    evaluator = DeepSeekEvaluator(key, budget, transport=httpx.MockTransport(handler))
    result = asyncio.run(
        evaluator.evaluate([candidate()], {"temperature_min": 20}, "daily")
    )
    return result, seen, evaluator


def response(content=None, **message):
    return {
        "id": "request-1",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": content
                    if content is not None
                    else json.dumps({"evaluations": [evaluation()]}),
                    **message,
                },
            }
        ],
        "usage": {"prompt_tokens": 300, "completion_tokens": 100},
    }


def test_pinned_projection_and_private_persistent_receipt(config):
    result, seen, _ = run(config, response())
    assert result[0]["candidate_id"] == "1"
    request = seen[0]
    assert str(request.url) == "https://api.deepseek.com/chat/completions"
    body = json.loads(request.content)
    assert body["model"] == "deepseek-flash" and body["thinking"] == {
        "type": "disabled"
    }
    assert (
        "PRIVATE NAME" not in request.content.decode()
        and "/private-image" not in request.content.decode()
    )
    assert body["response_format"] == {"type": "json_object"}
    assert config[1].reserved_calls() == 1
    assert config[1].path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    "body",
    [
        response(""),
        response("not json"),
        response('{"evaluations":[],"evaluations":[]}'),
        response(tool_calls=[{"name": "evil"}]),
        response(function_call={"name": "evil"}),
        response(refusal="no"),
        {"choices": []},
        {"choices": [response()["choices"][0], response()["choices"][0]]},
        {"choices": [{**response()["choices"][0], "finish_reason": "length"}]},
        response(json.dumps({"evaluations": [evaluation(999)]})),
        response(json.dumps({"evaluations": [evaluation(), evaluation()]})),
        response(
            json.dumps(
                {
                    "evaluations": [
                        {**evaluation(), "evidence": {"G001": ["waterproof"]}}
                    ]
                }
            )
        ),
        response(json.dumps({"evaluations": [{**evaluation(), "color": "0.8"}]})),
        response("synthetic-private-key-abcdef"),
        [],
        {"choices": [None]},
        {"choices": [{"message": []}]},
    ],
)
def test_adversarial_outputs_fail_closed_without_releasing_budget(config, body):
    with pytest.raises(BusinessError):
        run(config, body)
    assert config[1].reserved_calls() == 1


@pytest.mark.parametrize("status", [302, 401, 429, 500])
def test_network_status_is_explicit_and_redacted(config, status):
    with pytest.raises(BusinessError) as error:
        run(config, {"private_error": "synthetic-private-key-abcdef"}, status=status)
    assert "synthetic-private-key" not in error.value.message
    assert config[1].reserved_calls() == 1


def test_budget_is_atomic_across_instances_and_restart(tmp_path):
    path = tmp_path / "calls.sqlite"
    ModelCallQuota(path, 2, create=True)

    def reserve(_):
        try:
            ModelCallQuota(path, 2).reserve("hash")
            return True
        except BusinessError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(8))) == 2
    assert ModelCallQuota(path, 2).reserved_calls() == 2


def test_unconfigured_and_nonprivate_key_never_send(config):
    key, budget = config
    key.chmod(0o644)

    def forbidden(_):
        pytest.fail("credential failure must precede network")

    provider = DeepSeekEvaluator(key, budget, transport=httpx.MockTransport(forbidden))
    with pytest.raises(BusinessError):
        asyncio.run(provider.evaluate([candidate()], {}, "daily"))
    assert budget.reserved_calls() == 0


def test_production_composition_uses_configured_model(store):
    from wardrobe.app import create_app
    from wardrobe.config import Settings
    from wardrobe.recommendations import DisabledEvaluator

    db, settings, _ = store
    app = create_app(settings, database=db)
    assert isinstance(app.state.recommendations.evaluator, DisabledEvaluator)
    enabled = Settings(data_root=settings.data_root, model_provider="deepseek")
    with pytest.raises(ValueError, match="quota"):
        create_app(enabled, database=db)
    enabled = Settings(
        data_root=settings.data_root,
        model_provider="deepseek",
        model_credentials_file=settings.data_root / "model.json",
        model_call_limit=20,
        model_ledger_expected_used=0,
    )
    ModelCallQuota(settings.data_root / "model-calls.sqlite", 20, create=True)
    app = create_app(enabled, database=db)
    assert isinstance(app.state.recommendations.evaluator, DeepSeekEvaluator)


def test_settings_env_keeps_provider_pinned(monkeypatch, tmp_path):
    from wardrobe.config import Settings

    monkeypatch.setenv("WARDROBE_MODEL_PROVIDER", "deepseek")
    monkeypatch.setenv("WARDROBE_MODEL_CALL_LIMIT", "20")
    monkeypatch.setenv("WARDROBE_MODEL_LEDGER_EXPECTED_USED", "1")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    settings = Settings.from_env()
    assert settings.model_credentials_file == tmp_path / "model"
    assert settings.model_call_limit == 20
    assert settings.model_ledger_expected_used == 1
    monkeypatch.setenv("WARDROBE_MODEL_PROVIDER", "attacker")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_timeout_is_counted_once_and_never_retried(config):
    key, quota = config
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("private provider details", request=request)

    provider = DeepSeekEvaluator(key, quota, transport=httpx.MockTransport(handler))
    with pytest.raises(BusinessError) as error:
        asyncio.run(provider.evaluate([candidate()], {}, "daily"))
    assert error.value.code == "MODEL_PROVIDER_FAILED"
    assert len(calls) == quota.reserved_calls() == 1


def test_large_input_fails_before_quota_and_network(config):
    key, quota = config
    value = candidate()
    value["items"][0]["garment"]["annotation_fields"]["material"] = {
        "value": "x" * 100000
    }
    provider = DeepSeekEvaluator(key, quota)
    with pytest.raises(BusinessError) as error:
        asyncio.run(provider.evaluate([value], {}, "daily"))
    assert error.value.code == "MODEL_INPUT_TOO_LARGE"
    assert quota.reserved_calls() == 0


def test_escaped_credential_echo_rejected_before_receipt(config):
    key, quota = config
    body = json.dumps(response()).replace(
        "request-1", "".join(f"\\u{ord(c):04x}" for c in "synthetic-private-key-abcdef")
    )
    provider = DeepSeekEvaluator(
        key,
        quota,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)),
    )
    with pytest.raises(BusinessError):
        asyncio.run(provider.evaluate([candidate()], {}, "daily"))
    assert "synthetic-private-key" not in json.dumps(provider.last_receipt)


def test_response_limit_and_quota_exhaustion_precede_further_network(config):
    key, quota = config
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=b"x" * (1024 * 1024 + 1))

    provider = DeepSeekEvaluator(key, quota, transport=httpx.MockTransport(handler))
    for _ in range(3):
        with pytest.raises(BusinessError):
            asyncio.run(provider.evaluate([candidate()], {}, "daily"))
    assert len(calls) == quota.reserved_calls() == 2


def test_explicit_unlimited_keeps_ledger_and_limited_mode(tmp_path, monkeypatch):
    from wardrobe.config import Settings

    path = tmp_path / "calls.sqlite"
    capped = ModelCallQuota(path, 20, create=True)
    for i in range(20):
        capped.reserve(str(i))
    with pytest.raises(BusinessError, match="limit"):
        capped.reserve("over")
    unlimited = ModelCallQuota(path, "unlimited", expected_used=20)
    unlimited.reserve("twenty-one")
    assert unlimited.reserved_calls() == capped.reserved_calls() == 21
    monkeypatch.setenv("WARDROBE_MODEL_CALL_LIMIT", "unlimited")
    assert Settings.from_env().model_call_limit == "unlimited"
    monkeypatch.setenv("WARDROBE_MODEL_CALL_LIMIT", "20")
    assert Settings.from_env().model_call_limit == 20
    for bad in (None, "", "Unlimited", 0, -1, True):
        with pytest.raises((ValueError, TypeError)):
            ModelCallQuota(path, bad)
    with pytest.raises(ValueError):
        ModelCallQuota(path, "unlimited", expected_used=22)


def test_request_local_references_map_back_to_original_ids(config):
    key, quota = config
    value = candidate()
    value["candidate_id"] = "a1" * 32
    value["items"][0]["garment"]["id"] = "00000000-0000-4000-8000-000000000001"
    seen = []

    def handle(request):
        seen.append(request)
        model_value = {
            **evaluation(),
            "candidate_id": "C01",
            "evidence": {"G001": ["primary_color"]},
        }
        return httpx.Response(
            200, json=response(json.dumps({"evaluations": [model_value]}))
        )

    provider = DeepSeekEvaluator(key, quota, transport=httpx.MockTransport(handle))
    result = asyncio.run(provider.evaluate([value], {}, "daily"))
    wire = seen[0].content.decode()
    assert value["candidate_id"] not in wire
    assert value["items"][0]["garment"]["id"] not in wire
    assert result[0]["candidate_id"] == value["candidate_id"]
    assert result[0]["evidence"] == {
        value["items"][0]["garment"]["id"]: ["primary_color"]
    }


def test_failed_content_keeps_safe_reason_and_provider_usage(config):
    key, quota = config
    body = response("PRIVATE MALFORMED MODEL CONTENT")
    provider = DeepSeekEvaluator(
        key,
        quota,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
    )
    with pytest.raises(BusinessError):
        asyncio.run(provider.evaluate([candidate()], {}, "daily"))
    receipt = provider.last_receipt
    assert receipt["state"] == "invalid_output"
    assert receipt["validation_reason"] == "content_json"
    assert receipt["request_id"] == "request-1"
    assert receipt["usage"]["prompt_tokens"] == 300
    assert "PRIVATE MALFORMED" not in json.dumps(receipt)
    assert quota.reserved_calls() == 1


@pytest.mark.parametrize(
    ("values", "reason"),
    [
        ([{**evaluation(), "candidate_id": "C99"}], "candidate_unknown"),
        ([{**evaluation(), "candidate_id": "c01"}], "candidate_unknown"),
        ([evaluation(), evaluation()], "candidate_duplicate"),
        (
            [{**evaluation(), "evidence": {"G002": ["primary_color"]}}],
            "evidence_garment",
        ),
        ([{**evaluation(), "evidence": {"G001": ["waterproof"]}}], "evidence_field"),
        ([{**evaluation(), "evidence": {"G001": ["material"]}}], "evidence_field"),
        ([{**evaluation(), "color": 1.1}], "evaluation_schema"),
        (
            [{**evaluation(), "private_extra": "PRIVATE MODEL TEXT"}],
            "evaluation_schema",
        ),
    ],
)
def test_classified_failures_keep_strict_reference_and_evidence_binding(
    config, values, reason
):
    key, quota = config
    other = candidate(2)
    other["items"][0]["garment"]["id"] = "g2"
    first = candidate()
    first["items"][0]["garment"]["annotation_fields"]["material"] = {"value": None}
    body = response(json.dumps({"evaluations": values}))
    provider = DeepSeekEvaluator(
        key,
        quota,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
    )
    with pytest.raises(BusinessError) as error:
        asyncio.run(provider.evaluate([first, other], {}, "daily"))
    assert error.value.code == "INVALID_MODEL_OUTPUT"
    assert provider.last_receipt["validation_reason"] == reason
    assert provider.last_receipt["usage"]["completion_tokens"] == 100
    with quota.connect() as db:
        stored = json.loads(db.execute("SELECT receipt FROM attempts").fetchone()[0])
    assert stored == provider.last_receipt
    assert "PRIVATE" not in json.dumps(stored)
    assert quota.reserved_calls() == 1


def test_aliases_map_multiple_candidates_and_shared_garments_without_mutating_input(
    config,
):
    key, quota = config
    values = [candidate(3), candidate(2), candidate(1)]
    values[1]["items"][0]["garment"]["id"] = "g2"
    before = json.dumps(values)

    def handle(request):
        projection = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert len(projection["garments"]) == 2
        return httpx.Response(
            200,
            json=response(
                json.dumps(
                    {
                        "evaluations": [
                            {
                                **evaluation(),
                                "candidate_id": item["candidate_id"],
                                "evidence": {
                                    item["items"][0]["garment_id"]: ["primary_color"]
                                },
                            }
                            for item in reversed(projection["candidates"])
                        ]
                    }
                )
            ),
        )

    provider = DeepSeekEvaluator(key, quota, transport=httpx.MockTransport(handle))
    result = asyncio.run(provider.evaluate(values, {}, "daily"))
    by_id = {item["candidate_id"]: item for item in result}
    assert set(by_id) == {"1", "2", "3"}
    assert by_id["2"]["evidence"] == {"g2": ["primary_color"]}
    assert by_id["1"]["evidence"] == by_id["3"]["evidence"] == {"g1": ["primary_color"]}
    assert json.dumps(values) == before
    assert provider.last_receipt["evaluation_count"] == 3


def test_untrusted_receipt_metadata_is_bounded_and_filtered(config):
    body = response()
    body["id"] = "PRIVATE / arbitrary model text" * 100
    body["usage"] = {
        "prompt_tokens": True,
        "completion_tokens": -1,
        "total_tokens": 10**20,
        "private": "PRIVATE",
    }
    _, _, provider = run(config, body)
    assert "request_id" not in provider.last_receipt
    assert provider.last_receipt["usage"] == {}
    assert "PRIVATE" not in json.dumps(provider.last_receipt)


@pytest.mark.parametrize(
    "change, reason",
    [
        ({"usage": None}, "usage_shape"),
        ({"choices": []}, "choice_count"),
        (
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": "PRIVATE"},
                    }
                ]
            },
            "completion_status",
        ),
    ],
)
def test_envelope_failures_are_classified_without_body_logging(config, change, reason):
    key, quota = config
    body = {**response(), **change}
    provider = DeepSeekEvaluator(
        key,
        quota,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
    )
    with pytest.raises(BusinessError):
        asyncio.run(provider.evaluate([candidate()], {}, "daily"))
    assert provider.last_receipt["validation_reason"] == reason
    assert "PRIVATE" not in json.dumps(provider.last_receipt)


def test_o17_shortlist_twice_preserves_tau_then_layers_then_decimal_distance():
    from test_thermal_budget import garment, items, weather

    from wardrobe.outfit_provider import shortlist
    from wardrobe.thermal_budget import check_upper_budget, thermal_context

    ctx = thermal_context(weather("24", "24"))
    stacks = [
        [{"role": "upper", "garment": garment("upper", "medium")}],
        [{"role": "upper", "garment": garment("upper", "thin")}],
        [
            {"role": "base_layer", "garment": garment("upper", "thin")},
            {
                "role": "upper",
                "garment": garment(
                    "upper", "thin", category="短袖衬衫", identifier="shirt"
                ),
            },
        ],
    ]
    candidates = [
        {
            "candidate_id": str(n),
            "items": stack,
            "_first_allowed": True,
            "_thermal": check_upper_budget(stack, ctx),
        }
        for n, stack in enumerate(stacks)
    ]
    selected = shortlist(candidates)
    assert [c["candidate_id"] for c in selected] == ["2", "1", "0"]
    assert shortlist(selected) == selected
    # Same maximum rank and layer count: exact Decimal distance decides.
    winter = thermal_context(
        weather("16", "22.9", feels_like_median="19.4"), outerwear_required=True
    )
    options = [
        {
            "candidate_id": str(n),
            "items": stack,
            "_first_allowed": True,
            "_thermal": check_upper_budget(stack, winter),
        }
        for n, stack in enumerate([items("thin", "medium"), items("medium", "medium")])
    ]
    assert shortlist(options)[0]["candidate_id"] == "1"


def test_summer_safe_prompt_projection_keeps_only_finite_server_template(config):
    key, quota = config
    candidate_data = candidate()
    candidate_data["_layer_template"] = "summer_open_shirt"
    candidate_data["_guard"] = {"private_path": "/private-proof"}
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    model = DeepSeekEvaluator(key, quota, transport=httpx.MockTransport(handle))
    asyncio.run(model.evaluate([candidate_data], {}, "daily"))
    body = seen[0]
    assert "worn open" in body["messages"][0]["content"]
    data = json.loads(body["messages"][1]["content"])
    assert data["candidates"][0]["layer_template"] == "summer_open_shirt"
    assert "/private-proof" not in json.dumps(data) and "_guard" not in json.dumps(data)


def test_partial_thermal_metadata_cannot_fall_back_to_zero_distance():
    from wardrobe.outfit_provider import shortlist

    good = candidate()
    bad = candidate(2)
    bad["_first_allowed"] = True
    with pytest.raises(ValueError, match="Incomplete server thermal metadata"):
        shortlist([good, bad])


@pytest.mark.parametrize("access", [4, 5])
def test_systemd_credential_acl_requires_exact_service_only_access(monkeypatch, access):
    import os
    import struct
    from pathlib import Path

    from wardrobe.credential_permissions import _unit_acl_is_private

    undefined = 0xFFFFFFFF
    entries = [
        (1, access, undefined),
        (2, access, os.geteuid()),
        (4, 0, undefined),
        (16, access, undefined),
        (32, 0, undefined),
    ]

    def acl(values):
        return struct.pack("<I", 2) + b"".join(
            struct.pack("<HHI", *value) for value in values
        )

    path = Path("/run/credentials/open-wardrobe.service/model")
    monkeypatch.setattr(os, "getxattr", lambda *_: acl(entries), raising=False)
    assert _unit_acl_is_private(path, access)
    for index, replacement in [
        (1, (2, access, os.geteuid() + 1)),
        (2, (4, access, undefined)),
        (3, (16, 7, undefined)),
        (4, (32, access, undefined)),
    ]:
        wrong = list(entries)
        wrong[index] = replacement
        monkeypatch.setattr(os, "getxattr", lambda *_, value=wrong: acl(value))
        assert not _unit_acl_is_private(path, access)
    monkeypatch.setattr(os, "getxattr", lambda *_: b"malformed ACL")
    assert not _unit_acl_is_private(path, access)
