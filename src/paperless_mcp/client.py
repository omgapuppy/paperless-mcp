"""Typed asynchronous HTTP boundary for Paperless-ngx.

Only this module knows Paperless endpoint paths and wire response shapes.  Higher
layers receive validated payload models and never issue HTTP requests directly.
"""

from __future__ import annotations

import asyncio
import random
import ssl
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Annotated, Literal, TypeVar
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictInt,
    TypeAdapter,
    ValidationError,
)

from paperless_mcp.config import Settings
from paperless_mcp.errors import (
    AuthenticationError,
    InvalidTaxonomyError,
    NotFoundError,
    PaperlessConnectionError,
    PaperlessResponseError,
    RateLimitError,
    TLSVerificationError,
    UnsupportedPaperlessBehaviorError,
)
from paperless_mcp.logging import get_logger

API_VERSION = 10
RETRYABLE_GET_STATUSES = frozenset({429, 502, 503, 504})
MAX_RETRY_DELAY_SECONDS = 60.0
MAX_PAGINATION_REQUESTS = 1_000
PositiveWireId = Annotated[StrictInt, Field(gt=0)]

# The list endpoint is used for search and triage, never for OCR retrieval.  Keep
# this allowlist deliberately separate from the detail payload used by get_content.
DOCUMENT_LIST_FIELDS = (
    "id",
    "title",
    "created",
    "added",
    "modified",
    "correspondent",
    "document_type",
    "storage_path",
    "tags",
    "archive_serial_number",
    "original_file_name",
)

Sleep = Callable[[float], Awaitable[None]]
Jitter = Callable[[], float]
logger = get_logger(__name__)


class WireModel(BaseModel):
    """Validated Paperless wire data while tolerating additive server fields."""

    model_config = ConfigDict(extra="ignore")


class DocumentPayload(WireModel):
    id: PositiveWireId
    title: str
    content: str | None = None
    created: date | None = None
    added: AwareDatetime | None = None
    modified: AwareDatetime | None = None
    correspondent: PositiveWireId | None = None
    document_type: PositiveWireId | None = None
    storage_path: PositiveWireId | None = None
    tags: list[PositiveWireId] = Field(default_factory=list)
    archive_serial_number: int | None = Field(default=None, ge=0)
    original_file_name: str | None = None
    custom_fields: list[CustomFieldValuePayload] = Field(default_factory=list)
    notes: list[NotePayload] = Field(default_factory=list)


class CustomFieldValuePayload(WireModel):
    field: PositiveWireId
    value: JsonValue = None


class NotePayload(WireModel):
    id: PositiveWireId | None = None
    note: str
    created: AwareDatetime | None = None


class TaxonomyPayload(WireModel):
    id: PositiveWireId
    name: str = Field(min_length=1)
    slug: str | None = None
    parent: PositiveWireId | None = None
    children: list[TaxonomyPayload] = Field(default_factory=list)
    document_count: int | None = Field(default=None, ge=0)
    matching_algorithm: int | None = None
    is_insensitive: bool | None = None


class CustomFieldPayload(WireModel):
    id: PositiveWireId
    name: str = Field(min_length=1)
    data_type: str | None = None
    extra_data: JsonValue = None
    document_count: int | None = Field(default=None, ge=0)


class PagePayload(WireModel):
    count: int = Field(ge=0)
    next: str | None = None
    previous: str | None = None
    results: list[dict[str, JsonValue]]


class DocumentPagePayload(WireModel):
    count: int = Field(ge=0)
    next: str | None = None
    previous: str | None = None
    results: list[DocumentPayload]


class HealthPayload(WireModel):
    status: str | None = None


class VersionHeaders(WireModel):
    api_version: str | None
    server_version: str | None


_TAXONOMY_ADAPTER = TypeAdapter(list[TaxonomyPayload])
_CUSTOM_FIELDS_ADAPTER = TypeAdapter(list[CustomFieldPayload])
_NOTES_ADAPTER = TypeAdapter(list[NotePayload])

TaxonomyEndpoint = Literal[
    "tags",
    "correspondents",
    "document_types",
    "storage_paths",
    "custom_fields",
]
WireModelT = TypeVar("WireModelT", bound=WireModel)


class PaperlessClient:
    """Asynchronous, authenticated and version-pinned Paperless API client."""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        jitter: Jitter = random.random,
    ) -> None:
        self._settings = settings
        self._base_url = f"{settings.base_url}/"
        self._origin = _origin(self._base_url)
        self._api_path_prefix = urlsplit(urljoin(self._base_url, "api/")).path
        self._sleep = sleep
        self._jitter = jitter
        self._headers = {
            "Authorization": f"Token {settings.api_token}",
            "Accept": f"application/json; version={API_VERSION}",
        }
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            verify=settings.verify_tls,
            follow_redirects=False,
        )
        self._closed = False
        self._version_headers = VersionHeaders(api_version=None, server_version=None)

    @property
    def version_headers(self) -> VersionHeaders:
        """Return the most recently discovered Paperless version headers."""
        return self._version_headers.model_copy()

    async def __aenter__(self) -> PaperlessClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if not self._closed and self._owns_client:
            await self._client.aclose()
        self._closed = True

    async def health(self) -> tuple[HealthPayload, VersionHeaders]:
        response = await self._request_json("GET", "api/")
        payload = self._validate(HealthPayload, response, endpoint="api/")
        return payload, self.version_headers

    async def list_documents(self, params: Mapping[str, str | int]) -> DocumentPagePayload:
        request_params = dict(params)
        request_params["fields"] = ",".join(DOCUMENT_LIST_FIELDS)
        response = await self._request_json("GET", "api/documents/", params=request_params)
        try:
            return DocumentPagePayload.model_validate(response)
        except ValidationError as exc:
            raise PaperlessResponseError(
                "Paperless returned data with an unexpected shape.",
                details={"endpoint": "api/documents/"},
            ) from exc

    async def get_document(self, document_id: int) -> DocumentPayload:
        response = await self._request_json("GET", f"api/documents/{document_id}/")
        return self._validate(DocumentPayload, response, endpoint="api/documents/{id}/")

    async def patch_document(
        self,
        document_id: int,
        payload: Mapping[str, JsonValue],
    ) -> DocumentPayload:
        """Patch allowlisted metadata once; mutation requests are never retried."""
        response = await self._request_json(
            "PATCH",
            f"api/documents/{document_id}/",
            json=payload,
        )
        return self._validate(DocumentPayload, response, endpoint="api/documents/{id}/")

    async def list_document_notes(self, document_id: int) -> tuple[NotePayload, ...]:
        response = await self._request_json("GET", f"api/documents/{document_id}/notes/")
        if isinstance(response, dict):
            page = self._validate(
                PagePayload,
                response,
                endpoint="api/documents/{id}/notes/",
            )
            raw_notes: object = page.results
        else:
            raw_notes = response
        return tuple(
            self._validate_list(
                TypeAdapter(list[NotePayload]),
                raw_notes,
                endpoint="api/documents/{id}/notes/",
            )
        )

    async def add_document_note(self, document_id: int, note: str) -> tuple[NotePayload, ...]:
        """Create one note once; POST requests are never automatically retried."""
        response = await self._request_json(
            "POST",
            f"api/documents/{document_id}/notes/",
            json={"note": note},
        )
        return tuple(
            self._validate_list(
                _NOTES_ADAPTER,
                response,
                endpoint="api/documents/{id}/notes/",
            )
        )

    async def get_taxonomy_item(
        self,
        endpoint: TaxonomyEndpoint,
        item_id: int,
    ) -> TaxonomyPayload | CustomFieldPayload:
        response = await self._request_json("GET", f"api/{endpoint}/{item_id}/")
        if endpoint == "custom_fields":
            return self._validate(
                CustomFieldPayload,
                response,
                endpoint="api/custom_fields/{id}/",
            )
        return self._validate(
            TaxonomyPayload,
            response,
            endpoint=f"api/{endpoint}/{{id}}/",
        )

    async def taxonomy_item_exists(self, endpoint: TaxonomyEndpoint, item_id: int) -> bool:
        try:
            await self.get_taxonomy_item(endpoint, item_id)
        except NotFoundError:
            return False
        return True

    async def resolve_tag_id(self, name: str) -> int:
        """Resolve one exact tag name without accepting raw search syntax."""
        response = await self._request_json(
            "GET",
            "api/tags/",
            params={"name__iexact": name, "page_size": 2},
        )
        page = self._validate(PagePayload, response, endpoint="api/tags/")
        tags = self._validate_list(_TAXONOMY_ADAPTER, page.results, endpoint="api/tags/")
        exact = [tag for tag in tags if tag.name.casefold() == name.casefold()]
        if len(exact) != 1:
            raise InvalidTaxonomyError(
                "A tag name filter must resolve to exactly one existing Paperless tag."
            )
        return exact[0].id

    async def list_taxonomy(
        self,
        endpoint: TaxonomyEndpoint,
        *,
        max_items: int,
    ) -> tuple[TaxonomyPayload | CustomFieldPayload, ...]:
        raw_items = await self._collect_pages(
            f"api/{endpoint}/",
            params={"page_size": min(max_items, self._settings.max_page_size)},
            max_items=max_items,
        )
        if endpoint == "custom_fields":
            return tuple(
                self._validate_list(
                    _CUSTOM_FIELDS_ADAPTER,
                    raw_items,
                    endpoint=f"api/{endpoint}/",
                )
            )
        return tuple(
            self._validate_list(
                _TAXONOMY_ADAPTER,
                raw_items,
                endpoint=f"api/{endpoint}/",
            )
        )

    async def _collect_pages(
        self,
        endpoint: str,
        *,
        params: Mapping[str, str | int],
        max_items: int,
    ) -> list[dict[str, JsonValue]]:
        if max_items < 1:
            raise ValueError("max_items must be positive")

        response = await self._request_json("GET", endpoint, params=params)
        page = self._validate(PagePayload, response, endpoint=endpoint)
        results = list(page.results[:max_items])
        next_url = page.next
        request_count = 1
        visited_urls = {self._canonical_url(urljoin(self._base_url, endpoint), params=params)}
        while next_url is not None and len(results) < max_items:
            if request_count >= min(max_items, MAX_PAGINATION_REQUESTS):
                raise UnsupportedPaperlessBehaviorError(
                    "Paperless pagination exceeded the safe request limit."
                )
            next_url = self._safe_next_url(next_url)
            canonical_next_url = self._canonical_url(next_url)
            if canonical_next_url in visited_urls:
                raise UnsupportedPaperlessBehaviorError(
                    "Paperless returned a repeated pagination link."
                )
            visited_urls.add(canonical_next_url)
            response = await self._request_json("GET", next_url)
            request_count += 1
            page = self._validate(PagePayload, response, endpoint=endpoint)
            results.extend(page.results[: max_items - len(results)])
            next_url = page.next
        return results

    def _safe_next_url(self, next_url: str) -> str:
        try:
            absolute = urljoin(self._base_url, next_url)
            split = urlsplit(absolute)
            origin = _origin(absolute)
        except (TypeError, ValueError) as exc:
            raise UnsupportedPaperlessBehaviorError(
                "Paperless returned an invalid pagination link."
            ) from exc
        if split.username is not None or split.password is not None:
            raise UnsupportedPaperlessBehaviorError(
                "Paperless returned a pagination link containing user information."
            )
        if origin != self._origin:
            raise UnsupportedPaperlessBehaviorError(
                "Paperless returned a pagination link for a different origin."
            )
        if not split.path.startswith(self._api_path_prefix):
            raise UnsupportedPaperlessBehaviorError(
                "Paperless returned a pagination link outside the configured API path."
            )
        return absolute

    @staticmethod
    def _canonical_url(
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
    ) -> str:
        """Canonicalize a request target before using it as a pagination identity."""
        split = urlsplit(url)
        query_items = parse_qsl(split.query, keep_blank_values=True)
        if params is not None:
            query_items.extend((key, str(value)) for key, value in params.items())
        query = urlencode(sorted(query_items), doseq=True)
        netloc = split.hostname or ""
        if split.port is not None:
            netloc = f"{netloc}:{split.port}"
        return urlunsplit((split.scheme.casefold(), netloc.casefold(), split.path, query, ""))

    async def _request_json(
        self,
        method: str,
        endpoint: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json: Mapping[str, JsonValue] | None = None,
    ) -> object:
        if self._closed:
            raise PaperlessConnectionError("The Paperless client is closed.")

        url = urljoin(self._base_url, endpoint)
        attempts = self._settings.retry_attempts if method == "GET" else 0
        for retry_number in range(attempts + 1):
            started = time.monotonic()
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=self._headers,
                )
            except httpx.RequestError as exc:
                logger.warning(
                    "paperless_request_transport_error",
                    extra={
                        "operation": f"{method} {urlsplit(url).path}",
                        "duration_ms": round((time.monotonic() - started) * 1_000, 2),
                        "retry_count": retry_number,
                    },
                )
                if (
                    method == "GET"
                    and self._is_retryable_transport_error(exc)
                    and retry_number < attempts
                ):
                    await self._sleep(self._retry_delay(None, retry_number))
                    continue
                self._raise_connection_error(exc)

            self._discover_versions(response)
            logger.info(
                "paperless_request",
                extra={
                    "operation": f"{method} {urlsplit(url).path}",
                    "duration_ms": round((time.monotonic() - started) * 1_000, 2),
                    "status_code": response.status_code,
                    "retry_count": retry_number,
                },
            )
            if (
                method == "GET"
                and response.status_code in RETRYABLE_GET_STATUSES
                and retry_number < attempts
            ):
                await self._sleep(self._retry_delay(response, retry_number))
                continue

            self._raise_for_status(response)
            try:
                return response.json()
            except ValueError as exc:
                raise PaperlessResponseError(
                    "Paperless returned a response that was not valid JSON.",
                    details={"status_code": response.status_code},
                ) from exc

        raise AssertionError("retry loop did not return or raise")

    def _discover_versions(self, response: httpx.Response) -> None:
        api_version = response.headers.get("X-Api-Version")
        server_version = response.headers.get("X-Version")
        if api_version is not None or server_version is not None:
            self._version_headers = VersionHeaders(
                api_version=api_version or self._version_headers.api_version,
                server_version=server_version or self._version_headers.server_version,
            )

    def _retry_delay(self, response: httpx.Response | None, retry_number: int) -> float:
        retry_after = (
            _parse_retry_after(response.headers.get("Retry-After"))
            if response is not None
            else None
        )
        if retry_after is not None:
            return min(retry_after, MAX_RETRY_DELAY_SECONDS)
        exponential = float(2**retry_number)
        return min(exponential + self._jitter(), MAX_RETRY_DELAY_SECONDS)

    @staticmethod
    def _raise_connection_error(exc: httpx.RequestError) -> None:
        for current in _exception_chain(exc):
            if isinstance(current, ssl.SSLError):
                raise TLSVerificationError(
                    "TLS verification failed while connecting to Paperless."
                ) from exc
        raise PaperlessConnectionError("Could not connect to Paperless.") from exc

    @staticmethod
    def _is_retryable_transport_error(exc: httpx.RequestError) -> bool:
        """Return whether a failed idempotent request may be retried safely."""
        if any(isinstance(current, ssl.SSLError) for current in _exception_chain(exc)):
            return False
        return isinstance(exc, (httpx.ConnectError, httpx.TimeoutException, httpx.PoolTimeout))

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        if status in {401, 403}:
            raise AuthenticationError(
                "Paperless rejected the configured API credentials.",
                details={"status_code": status},
            )
        if status == 404:
            raise NotFoundError(
                "The requested Paperless resource was not found.",
                details={"status_code": status},
            )
        if status == 429:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            raise RateLimitError(
                "Paperless rate limit was reached.",
                retry_after_seconds=retry_after,
            )
        if status == 406:
            raise UnsupportedPaperlessBehaviorError(
                f"Paperless does not accept API version {API_VERSION}.",
                details={"status_code": status},
            )
        raise PaperlessResponseError(
            "Paperless returned an unsuccessful response.",
            details={"status_code": status},
        )

    @staticmethod
    def _validate(
        model: type[WireModelT],
        value: object,
        *,
        endpoint: str,
    ) -> WireModelT:
        try:
            return model.model_validate(value)
        except ValidationError as exc:
            raise PaperlessResponseError(
                "Paperless returned data with an unexpected shape.",
                details={"endpoint": endpoint},
            ) from exc

    @staticmethod
    def _validate_list(
        adapter: TypeAdapter[list[WireModelT]],
        value: object,
        *,
        endpoint: str,
    ) -> list[WireModelT]:
        try:
            return adapter.validate_python(value)
        except ValidationError as exc:
            raise PaperlessResponseError(
                "Paperless returned data with an unexpected shape.",
                details={"endpoint": endpoint},
            ) from exc


def _origin(url: str) -> tuple[str, str, int | None]:
    split = urlsplit(url)
    scheme = split.scheme.casefold()
    port = split.port
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return scheme, (split.hostname or "").casefold(), port


def _exception_chain(exc: BaseException) -> tuple[BaseException, ...]:
    """Return a bounded exception cause/context chain without looping."""
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and len(chain) < 10 and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return tuple(chain)


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0.0, (parsed - datetime.now(UTC)).total_seconds())
