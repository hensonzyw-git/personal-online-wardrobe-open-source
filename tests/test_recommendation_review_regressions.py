"""Synthetic reproductions of the independent v0.4.0 review findings."""

import asyncio
import json
import sqlite3
import tomllib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from wardrobe.app import create_app
from wardrobe.errors import BusinessError
from wardrobe.outfit_provider import (
    DeepSeekEvaluator,
    ModelCallQuota,
    shortlist,
)
from wardrobe.recommendations import RecommendationInput, candidates_for


def catalog(*, cold=False):
    return [
        {
            "id": f"{role}-{index:03}",
            "roles": [role],
            "category": {
                "upper": "短袖T恤",
                "lower": "长裤",
                "outerwear": "外套",
                "footwear": "低帮运动鞋",
            }[role],
            "recommendation_eligible": True,
            "annotation_fields": {
                "sleeve": {"value": "短袖" if role == "upper" else "长袖"},
                "thickness": {
                    "value": "thick" if cold and role != "footwear" else "thin",
                    "verification_state": "user_verified",
                },
            },
            "availability": "unknown",
        }
        for role, amount in [
            ("upper", 70),
            ("lower", 26),
            ("footwear", 17),
            ("outerwear", 14),
        ]
        for index in range(amount)
    ]


def payload(**kwargs):
    return RecommendationInput(
        location={
            "name": "synthetic",
            "latitude": 30.0,
            "longitude": 120.0,
            "timezone": "UTC",
        },
        **kwargs,
    )


def coverage(candidates, role):
    return {
        i["garment"]["id"] for c in candidates for i in c["items"] if i["role"] == role
    }


def test_large_catalog_covers_all_upper_lower_and_footwear():
    candidates, search = candidates_for(
        catalog(), payload(), SimpleNamespace(feels_like_min=24, feels_like_max=26)
    )
    assert len(coverage(candidates, "upper")) == 70
    assert len(coverage(candidates, "lower")) == 26
    assert len(coverage(candidates, "footwear")) == 17
    assert len(coverage(shortlist(candidates), "upper")) == 24
    assert search["searched_variants"] <= 500


@pytest.mark.parametrize(
    "core", ["upper-069", "lower-025", "footwear-016", "outerwear-013"]
)
def test_core_outside_old_uuid_window_is_still_reachable(core):
    candidates, _ = candidates_for(
        catalog(),
        payload(core_ids=[core]),
        SimpleNamespace(feels_like_min=24, feels_like_max=26),
    )
    assert candidates
    assert all(core in {i["garment"]["id"] for i in c["items"]} for c in candidates)


def test_missing_ledger_cannot_silently_reset_deployed_quota(tmp_path):
    path = tmp_path / "missing.sqlite"
    with pytest.raises((ValueError, OSError, BusinessError)):
        ModelCallQuota(path, 20)
    assert not path.exists()


def test_openapi_version_matches_package_version(store):
    db, settings, _ = store
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert (
        create_app(settings, database=db).openapi()["info"]["version"]
        == project["project"]["version"]
    )


def provider(tmp_path, count=1, status=200):
    path = tmp_path / "calls.sqlite"
    quota = ModelCallQuota(path, 20, create=True)
    key = tmp_path / "key.json"
    key.write_text(json.dumps({"api_key": "synthetic-model-key-123456"}))
    key.chmod(0o600)
    candidates = [
        {
            "candidate_id": str(i),
            "items": [
                {
                    "role": "upper",
                    "garment": {
                        "id": "g",
                        "category": "shirt",
                        "annotation_fields": {},
                    },
                }
            ],
        }
        for i in range(count)
    ]
    evaluations = [
        {
            "candidate_id": f"C{i + 1:02d}",
            "suitable": True,
            "color": 0.8,
            "material": 0.5,
            "style": 0.8,
            "silhouette": 0.5,
            "evidence": {},
        }
        for i in range(count)
    ]
    body = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps({"evaluations": evaluations}),
                },
            }
        ]
    }
    model = DeepSeekEvaluator(
        key,
        quota,
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body)),
    )
    return model, candidates


def test_thirteen_model_evaluations_are_rejected(tmp_path):
    model, candidates = provider(tmp_path, 13)
    with pytest.raises(BusinessError) as error:
        asyncio.run(model.evaluate(candidates, {}, "daily"))
    assert error.value.code == "INVALID_MODEL_OUTPUT"


def test_receipt_io_failure_preserves_classified_provider_error(tmp_path, monkeypatch):
    model, candidates = provider(tmp_path, status=429)

    def failed(*_):
        raise sqlite3.OperationalError("private path must not be logged")

    monkeypatch.setattr(model.budget, "receipt", failed)
    with pytest.raises(BusinessError) as error:
        asyncio.run(model.evaluate(candidates, {}, "daily"))
    assert error.value.code == "MODEL_RATE_LIMITED"
    assert model.budget.reserved_calls() == 1


def test_cold_catalog_covers_every_outerwear_and_reports_actual_coverage():
    candidates, search = candidates_for(
        catalog(cold=True),
        payload(),
        SimpleNamespace(feels_like_min=5, feels_like_max=9),
    )
    for role, count in [
        ("upper", 70),
        ("lower", 26),
        ("footwear", 17),
        ("outerwear", 14),
    ]:
        assert len(coverage(candidates, role)) == count
        assert search["candidate_role_coverage"][role] == count
        assert search["eligible_role_counts"][role] == count
    assert search["search_truncated"] is True and search["searched_variants"] == 500


def test_multiple_cores_and_impossible_core_assignments():
    weather = SimpleNamespace(feels_like_min=24, feels_like_max=26)
    cores = ["upper-069", "lower-025", "footwear-016", "outerwear-013"]
    candidates, search = candidates_for(catalog(), payload(core_ids=cores), weather)
    assert len(candidates) == 1 and not search["search_truncated"]
    assert set(cores) == {i["garment"]["id"] for i in candidates[0]["items"]}
    for cores in [["missing"], ["upper-068", "upper-069"]]:
        assert not candidates_for(catalog(), payload(core_ids=cores), weather)[0]


def test_one_piece_templates_are_interleaved_and_core_can_select_them():
    garments = catalog() + [
        {**catalog()[0], "id": f"piece-{i:03}", "roles": ["one_piece"]}
        for i in range(10)
    ]
    weather = SimpleNamespace(feels_like_min=24, feels_like_max=26)
    candidates, _ = candidates_for(garments, payload(), weather)
    assert len(coverage(candidates, "one_piece")) == 10
    assert len(coverage(candidates, "upper")) == 70
    constrained, _ = candidates_for(garments, payload(core_ids=["piece-009"]), weather)
    assert constrained and all(
        "piece-009" in {i["garment"]["id"] for i in c["items"]} for c in constrained
    )


def test_order_independence_and_overlapping_roles_never_duplicate_an_item():
    garments = [
        {**catalog()[0], "id": str(i), "roles": ["upper", "lower", "footwear"]}
        for i in range(5)
    ]
    weather = SimpleNamespace(feels_like_min=24, feels_like_max=26)
    first, search = candidates_for(garments, payload(core_ids=["4"]), weather)
    second, _ = candidates_for(
        list(reversed(garments)), payload(core_ids=["4"]), weather
    )
    assert first and [c["signature"] for c in first] == [c["signature"] for c in second]
    assert all(len({i["garment"]["id"] for i in c["items"]}) == 3 for c in first)
    assert len({c["signature"] for c in first}) == len(first)
    assert search["searched_variants"] <= 500


def test_hard_constraints_filter_roles_before_sampling():
    garments = catalog()
    for g in garments:
        g["annotation_fields"] = {
            "formality": {"value": "formal"},
            "warmth": {"value": "low", "verification_state": "user_verified"},
            "thickness": {"value": "thin", "verification_state": "user_verified"},
        }
        if g["id"] == "footwear-016":
            g["annotation_fields"]["waterproof"] = {
                "value": True,
                "verification_state": "user_verified",
            }
    weather = SimpleNamespace(feels_like_min=29, feels_like_max=32)
    candidates, _ = candidates_for(
        garments,
        payload(occasion="formal", required_functions={"footwear": ["waterproof"]}),
        weather,
    )
    assert candidates and coverage(candidates, "footwear") == {"footwear-016"}
    garments[69]["annotation_fields"]["warmth"]["value"] = "high"
    assert not candidates_for(garments, payload(core_ids=["upper-069"]), weather)[0]


def test_ledger_initialization_and_continuity_assertions(tmp_path):
    path = tmp_path / "calls.sqlite"
    first = ModelCallQuota(path, 20, create=True)
    with pytest.raises(ValueError, match="continuity"):
        ModelCallQuota(path, 20, expected_used=1)
    first.reserve("synthetic")
    assert ModelCallQuota(path, 20, expected_used=1).reserved_calls() == 1
    with pytest.raises(ValueError):
        ModelCallQuota(path, 20, create=True)
    assert ModelCallQuota(path, 20, expected_used=1).reserved_calls() == 1
    first.reserve("synthetic2")
    assert ModelCallQuota(path, 20, expected_used=1).reserved_calls() == 2
    path.unlink()
    with pytest.raises((ValueError, sqlite3.Error)):
        first.reserve("synthetic3")
    assert not path.exists()


def test_runtime_refuses_empty_or_missing_transferred_ledger(store):
    from wardrobe.config import Settings

    db, settings, _ = store
    configured = Settings(
        data_root=settings.data_root,
        model_provider="deepseek",
        model_credentials_file=settings.data_root / "unused.json",
        model_call_limit=20,
        model_ledger_expected_used=1,
    )
    with pytest.raises(ValueError):
        create_app(configured, database=db)
    assert not (settings.data_root / "model-calls.sqlite").exists()
    quota = ModelCallQuota(settings.data_root / "model-calls.sqlite", 20, create=True)
    with pytest.raises(ValueError, match="continuity"):
        create_app(configured, database=db)
    quota.reserve("synthetic-transferred-usage")
    app = create_app(configured, database=db)
    assert app.state.recommendations.evaluator.budget.reserved_calls() == 1


def test_receipt_failure_is_redacted_and_keeps_valid_result(
    tmp_path, monkeypatch, caplog
):
    model, candidates = provider(tmp_path)

    def failed(*_):
        raise sqlite3.OperationalError("private path must not be logged")

    monkeypatch.setattr(model.budget, "receipt", failed)
    assert len(asyncio.run(model.evaluate(candidates, {}, "daily"))) == 1
    assert model.last_receipt["metadata_persisted"] is False
    assert model.budget.reserved_calls() == 1
    assert "private path" not in caplog.text


def test_twelve_model_evaluations_are_accepted(tmp_path):
    model, candidates = provider(tmp_path, 12)
    assert len(asyncio.run(model.evaluate(candidates, {}, "daily"))) == 12


def test_operator_cli_initializes_once_and_cannot_reset_usage(
    tmp_path, capsys, monkeypatch
):
    import sys

    from wardrobe.cli import main

    root = tmp_path / "new-deployment"
    command = ["--data-root", str(root), "init-model-ledger", "--call-limit", "20"]
    monkeypatch.setattr(sys, "argv", ["wardrobectl", *command])
    main()
    quota = ModelCallQuota(root / "model-calls.sqlite", 20)
    quota.reserve("synthetic-cli-usage")
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert (
        ModelCallQuota(
            root / "model-calls.sqlite", 20, expected_used=1
        ).reserved_calls()
        == 1
    )
    assert not (root / "wardrobe.sqlite").exists()
    assert "never overwritten" in capsys.readouterr().out
