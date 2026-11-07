"""Test doubles shared between the API suites."""

from __future__ import annotations

from typing import Any


class FakeResponse:
    def __init__(
        self,
        status: int,
        payload: Any,
        headers: dict | None = None,
        text: str | Exception = "",
    ) -> None:
        self.status, self._payload = status, payload
        self.headers = headers or {}
        # A body the JSON parser could not read. Pass an exception to stand in
        # for one that cannot even be decoded to a string.
        self._text = text

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def json(self, **kwargs: Any) -> Any:
        # content_type=None is how the callers ask aiohttp to parse a body
        # whatever the server labelled it; accept and ignore it. A payload
        # that is an exception stands in for a body the parser choked on.
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def text(self) -> str:
        if isinstance(self._text, Exception):
            raise self._text
        return self._text


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response

    def request(self, *args: object, **kwargs: object) -> FakeResponse:
        return self._response

    def post(self, *args: object, **kwargs: object) -> FakeResponse:
        return self._response

    def get(self, *args: object, **kwargs: object) -> FakeResponse:
        return self._response


class ScriptedSession:
    """Answers each request with the next scripted response, and records it.

    For clients that make several calls in a row where the order matters —
    a retry after a challenge, a sign-in that polls — and the test wants to
    see what was sent each time, not only what came back.
    """

    def __init__(self, *responses: FakeResponse | Exception) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _next(self, method: str, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._next("POST", url, kwargs)

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._next("GET", url, kwargs)
