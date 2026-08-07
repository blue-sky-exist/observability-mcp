from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings, tls_context
from .errors import ToolError
from .http_auth import httpx_auth_and_headers
from .version import __version__


class ElasticsearchClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        auth, headers = httpx_auth_and_headers(settings.es_auth)
        headers["User-Agent"] = f"observability-mcp/{__version__}"
        self._client = httpx.AsyncClient(
            auth=auth,
            headers=headers,
            timeout=settings.es_timeout,
            verify=tls_context(settings.es_verify_tls, settings.es_ca_file),
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        accepted_statuses: set[int] | None = None,
    ) -> dict[str, Any]:
        failures: list[str] = []
        for base_url in self.settings.es_urls:
            try:
                response = await self._client.request(
                    method,
                    f"{base_url}{path}",
                    json=body if body is not None else None,
                )
            except httpx.TimeoutException:
                failures.append(f"{base_url}: request exceeded {self.settings.es_timeout:g}s")
                continue
            except httpx.RequestError as exc:
                failures.append(f"{base_url}: {exc}")
                continue

            if response.is_success or (
                accepted_statuses and response.status_code in accepted_statuses
            ):
                try:
                    return response.json()
                except ValueError:
                    failures.append(f"{base_url}: response is not valid JSON")
                    continue

            detail = response.text.strip().replace("\n", " ")[:1000]
            failures.append(f"{base_url}: HTTP {response.status_code} {detail or 'empty response'}")

        raise ToolError("All Elasticsearch nodes failed: " + " | ".join(failures))

    async def open_pit(self, index: str) -> str:
        index_path = quote(index, safe="*,-_:@")
        keep_alive = quote(self.settings.es_pit_keep_alive, safe="")
        payload = await self.request("POST", f"/{index_path}/_pit?keep_alive={keep_alive}")
        pit_id = payload.get("id")
        if not isinstance(pit_id, str) or not pit_id:
            raise ToolError("Elasticsearch did not return a valid PIT id")
        return pit_id

    async def resolve_indices(self, index_expression: str) -> dict[str, Any]:
        index_path = quote(index_expression, safe="*,-_:@")
        return await self.request(
            "GET",
            f"/_resolve/index/{index_path}"
            "?expand_wildcards=all&ignore_unavailable=true&allow_no_indices=true",
        )

    async def search_pit(
        self,
        pit_id: str,
        body: dict[str, Any],
        search_after: list[Any] | None = None,
    ) -> dict[str, Any]:
        request_body = dict(body)
        request_body["pit"] = {
            "id": pit_id,
            "keep_alive": self.settings.es_pit_keep_alive,
        }
        if search_after is not None:
            request_body["search_after"] = search_after
        try:
            return await self.request("POST", "/_search", request_body)
        except ToolError as exc:
            if "search_context_missing_exception" in str(exc):
                raise ToolError("Elasticsearch PIT expired; start a new search") from exc
            raise

    async def close_pit(self, pit_id: str) -> None:
        try:
            await self.request("DELETE", "/_pit", {"id": pit_id}, accepted_statuses={404})
        except ToolError:
            return
