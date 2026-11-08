"""Suite-wide guards: no test may open a network socket or write a Hypothesis database."""

from __future__ import annotations

import socket

import pytest
from hypothesis import settings

settings.register_profile("suite", database=None, deadline=None)
settings.load_profile("suite")


class NetworkBlocked(AssertionError):
    pass


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    attempts: list[tuple] = []

    def refuse(*args, **kwargs):
        attempts.append(args)
        raise NetworkBlocked("network access attempted during a test")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    return attempts
