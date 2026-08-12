import asyncio
import copy
import json
from collections.abc import Iterable
from typing import Any

import pytest

from serve.api_fastapi import MAX_REQUEST_BYTES, app


VALID_REQUEST = {
    "horizons_hours": [1, 6],
    "sources": [
        {
            "name": "radar",
            "members_mm": [[0, 1], [2, 5]],
            "recent_crps": [1, 2],
        }
    ],
    "thresholds_mm": [1, 10],
    "quantiles": [0.1, 0.5, 0.9],
}


def _request(
    *,
    payload: dict[str, Any] | None = None,
    chunks: Iterable[bytes] | None = None,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[int, dict[str, Any], int]:
    body_chunks = list(chunks) if chunks is not None else [json.dumps(payload).encode()]
    request_headers = [(b"content-type", b"application/json")]
    if headers:
        request_headers.extend(headers)
    receive_calls = 0
    sent: list[dict[str, Any]] = []

    async def run() -> None:
        nonlocal receive_calls
        pending = list(body_chunks)

        async def receive() -> dict[str, Any]:
            nonlocal receive_calls
            receive_calls += 1
            if not pending:
                return {"type": "http.disconnect"}
            body = pending.pop(0)
            return {
                "type": "http.request",
                "body": body,
                "more_body": bool(pending),
            }

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/marginals",
            "raw_path": b"/v1/marginals",
            "query_string": b"",
            "root_path": "",
            "headers": request_headers,
            "client": ("test-client", 50000),
            "server": ("test-server", 80),
            "state": {},
        }
        await app(scope, receive, send)

    asyncio.run(run())
    start = next(message for message in sent if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return start["status"], json.loads(response_body), receive_calls


def test_valid_request_preserves_marginal_endpoint_contract():
    status, response, _ = _request(payload=VALID_REQUEST)

    assert status == 200
    assert response["method"] == "evidence_weighted_marginals_v1"
    assert response["horizons_hours"] == [1, 6]


@pytest.mark.parametrize(
    "mutate, expected_error",
    [
        (lambda body: body.update(horizons_hours=["1", 6]), "int_type"),
        (
            lambda body: body["sources"][0].update(members_mm=[[False, 1], [2, 5]]),
            "float_type",
        ),
        (lambda body: body["sources"][0].update(age_hours="0"), "float_type"),
        (lambda body: body.update(unexpected=True), "extra_forbidden"),
        (
            lambda body: body["sources"][0].update(unexpected=True),
            "extra_forbidden",
        ),
    ],
)
def test_request_schema_rejects_coercion_and_unknown_fields(mutate, expected_error):
    payload = copy.deepcopy(VALID_REQUEST)
    mutate(payload)

    status, response, _ = _request(payload=payload)

    assert status == 422
    assert expected_error in {error["type"] for error in response["detail"]}


def test_declared_oversize_body_is_rejected_without_reading_it():
    status, response, receive_calls = _request(
        chunks=[b"{}"],
        headers=[(b"content-length", str(MAX_REQUEST_BYTES + 1).encode())],
    )

    assert status == 413
    assert response == {"detail": "request body too large"}
    assert receive_calls == 0


def test_chunked_oversize_body_aborts_on_the_first_excess_chunk():
    status, response, receive_calls = _request(
        chunks=[b"x" * 600_000, b"y" * 400_001, b"must-not-be-read"]
    )

    assert status == 413
    assert response == {"detail": "request body too large"}
    assert receive_calls == 2


@pytest.mark.parametrize(
    "header",
    [[(b"content-length", b"not-a-number")], [(b"content-length", b"-1")]],
)
def test_invalid_content_length_is_rejected(header):
    status, response, receive_calls = _request(chunks=[b"{}"], headers=header)

    assert status == 400
    assert response == {"detail": "invalid content-length"}
    assert receive_calls == 0
