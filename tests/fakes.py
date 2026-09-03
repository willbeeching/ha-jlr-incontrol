"""Test doubles shared between the API suites."""

from __future__ import annotations

from typing import Any


class FakeResponse:
    def __init__(self, status: int, payload: Any, headers: dict | None = None) -> None:
        self.status, self._payload = status, payload
        self.headers = headers or {}

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def json(self) -> Any:
        return self._payload

    async def text(self) -> str:
        return ""


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response

    def request(self, *args: object, **kwargs: object) -> FakeResponse:
        return self._response
