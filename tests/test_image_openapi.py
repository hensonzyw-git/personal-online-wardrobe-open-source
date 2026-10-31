"""A connector must discover real binary image responses without making images public."""

import io

import pytest
from conftest import TOKEN_B
from PIL import Image
from test_catalog_writes import create, enable_write, upload
from test_foundation import headers


@pytest.mark.parametrize("fmt,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg")])
def test_connector_schema_matches_authenticated_image_bytes(live_service, fmt, mime):
    client, db = live_service
    enable_write(db)
    raw = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(raw, format=fmt)
    asset_id = upload(client, raw.getvalue())
    garment_id = create(client, asset_id)["data"]["garment_id"]
    detail = client.get("/v1/garments/" + garment_id, headers=headers()).json()["data"][
        "garment"
    ]
    reference = detail["image_references"][0]
    schema_response = client.get("/openapi.json", headers=headers())
    assert schema_response.status_code == 200
    schema = schema_response.json()
    for path in [
        "/v1/assets/{asset_id}/content",
        "/v1/product-drafts/{draft_id}/assets/{asset_id}/content",
    ]:
        response = schema["paths"][path]["get"]["responses"]["200"]
        assert set(response["content"]) == {"image/png", "image/jpeg", "image/heic", "image/heif"}
        assert response["content"][mime]["schema"] == {
            "type": "string",
            "format": "binary",
        }
        assert (
            "private, no-store" in response["headers"]["Cache-Control"]["description"]
        )
    response = client.get(reference["content_url"], headers=headers())
    assert (
        response.status_code == 200
        and response.headers["content-type"] == mime == reference["mime"]
    )
    assert response.content == raw.getvalue()
    assert response.headers["cache-control"] == "private, no-store"
    assert client.get(reference["content_url"]).status_code == 401
    assert (
        client.get(reference["content_url"], headers=headers(TOKEN_B)).status_code
        == 404
    )
