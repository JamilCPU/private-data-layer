"""A fake imaplib connection that records calls and replays canned responses."""

from __future__ import annotations

from typing import Any


class FakeImap:
    def __init__(
        self,
        *,
        status: bytes = b"INBOX (UIDVALIDITY 777 UIDNEXT 12)",
        search: bytes = b"10 11",
        fetch_items: list[Any] | None = None,
        statuses: dict[str, tuple[str, bytes]] | None = None,
    ) -> None:
        # Canned (status, text) per command: login/select/status/SEARCH/FETCH.
        self.statuses = statuses or {}
        self.calls: list[tuple[Any, ...]] = []
        self.status_resp = status
        self.search_resp = search
        self.fetch_items = fetch_items or []
        self.logout_error: Exception | None = None

    def __call__(self, host: str, port: int) -> FakeImap:
        self.calls.append(("connect", host, port))
        return self

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.calls.append(("login", user, password))
        if "login" in self.statuses:
            st, text = self.statuses["login"]
            return st, [text]
        return "OK", [b"ok"]

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(("select", mailbox, readonly))
        if "select" in self.statuses:
            st, text = self.statuses["select"]
            return st, [text]
        return "OK", [b"2"]

    def status(self, mailbox: str, what: str) -> tuple[str, list[bytes]]:
        self.calls.append(("status", mailbox, what))
        if "status" in self.statuses:
            st, text = self.statuses["status"]
            return st, [text]
        return "OK", [self.status_resp]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self.calls.append(("uid", command, *args))
        if command in self.statuses:
            st, text = self.statuses[command]
            if st != "OK":
                return st, [text]
        if command == "SEARCH":
            return "OK", [self.search_resp]
        return "OK", list(self.fetch_items)

    def logout(self) -> tuple[str, list[bytes]]:
        self.calls.append(("logout",))
        if self.logout_error:
            raise self.logout_error
        return "BYE", [b"bye"]
