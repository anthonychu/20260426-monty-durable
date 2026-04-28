from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aiohttp
import jmespath
from azure.identity.aio import DefaultAzureCredential
from pydantic import BaseModel, Field


_credential: DefaultAzureCredential | None = None


class AzureRestParams(BaseModel):
    path: str = Field(
        description="ARM REST API path relative to https://management.azure.com. Must include api-version."
    )
    method: str = Field(default="GET", description="HTTP method. The prototype currently allows only GET.")
    body: Any | None = Field(default=None, description="Request body. Disabled for the read-only prototype.")
    query: str | None = Field(default=None, description="Optional JMESPath query applied to the JSON response.")


async def azure_rest(raw_params: dict[str, Any]) -> Any:
    params = _validate_params(raw_params)
    token = await _get_credential().get_token("https://management.azure.com/.default")

    headers = {
        "Authorization": f"Bearer {token.token}",
        "Content-Type": "application/json",
    }

    async with aiohttp.ClientSession() as session:
        async with session.request(params.method.upper(), f"https://management.azure.com{params.path}", headers=headers) as response:
            data = await _read_response(response)

            if response.status >= 400:
                body = json.dumps(data, default=str)[:2000]
                raise RuntimeError(f"ARM request failed with HTTP {response.status}: {body}")

            if params.query:
                try:
                    data = jmespath.search(params.query, data)
                except Exception as exc:
                    raise ValueError(f"JMESPath query failed: {exc}") from exc

            return data


def _validate_params(raw_params: dict[str, Any]) -> AzureRestParams:
    params = _model_validate(raw_params)
    params.method = params.method.upper()

    if params.method != "GET":
        raise ValueError("Only GET requests are enabled for the azure_rest prototype activity.")
    if params.body is not None:
        raise ValueError("Request bodies are disabled for the azure_rest prototype activity.")

    parsed_path = urlsplit(params.path)
    if parsed_path.scheme or parsed_path.netloc:
        raise ValueError("Azure REST path must be relative to https://management.azure.com.")
    if not parsed_path.path.startswith("/"):
        raise ValueError("Azure REST path must start with '/'.")
    if "api-version" not in parse_qs(parsed_path.query):
        raise ValueError("Azure REST path must include an api-version query parameter.")

    return params


def _model_validate(raw_params: dict[str, Any]) -> AzureRestParams:
    if hasattr(AzureRestParams, "model_validate"):
        return AzureRestParams.model_validate(raw_params)
    return AzureRestParams.parse_obj(raw_params)


def _get_credential() -> DefaultAzureCredential:
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential()
    return _credential


async def _read_response(response: aiohttp.ClientResponse) -> Any:
    try:
        return await response.json(content_type=None)
    except Exception:
        return {"raw": await response.text()}