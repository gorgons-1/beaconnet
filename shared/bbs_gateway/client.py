"""Client for the bbs-core internal API. One shared HTTP pool per gateway process."""

from __future__ import annotations

from typing import Any

import httpx


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class CoreAPI:
    def __init__(self, base_url: str, service_token: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-Service-Token": service_token, "User-Agent": "bbs-gateway/1.0"},
            timeout=httpx.Timeout(10.0, connect=5.0),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
            transport=transport,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def call(self, method: str, path: str, *, session: str | None = None, json: Any = None,
                   params: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {session}"} if session else {}
        if params:
            params = {k: v for k, v in params.items() if v is not None and v != ""}
        try:
            r = await self._http.request(method, path, json=json, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise ApiError("core_unavailable", "the BBS core is not responding; try again shortly", 503) from exc
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            err = data.get("error", {}) if isinstance(data, dict) else {}
            raise ApiError(err.get("code", "error"), err.get("message", f"HTTP {r.status_code}"), r.status_code)
        return data


class Session:
    """A person's session: the token is issued by bbs-core and only ever sent back to it."""

    def __init__(self, api: CoreAPI, token: str, kind: str, idle_timeout_s: int) -> None:
        self.api = api
        self.token = token
        self.kind = kind
        self.idle_timeout_s = idle_timeout_s

    async def get(self, path: str, **params: Any) -> dict:
        return await self.api.call("GET", path, session=self.token, params=params)

    async def post(self, path: str, body: dict | None = None) -> dict:
        return await self.api.call("POST", path, session=self.token, json=body or {})

    async def delete(self, path: str) -> dict:
        return await self.api.call("DELETE", path, session=self.token)

    async def end(self) -> None:
        try:
            await self.delete("/v1/sessions/me")
        except ApiError:
            pass
