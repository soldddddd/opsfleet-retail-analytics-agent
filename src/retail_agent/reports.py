import hashlib
import json
import os
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from retail_agent.auth import Principal
from retail_agent.errors import AgentError, ErrorCode
from retail_agent.models import Report
from retail_agent.privacy import sanitize_text


@dataclass(frozen=True)
class SavedReport:
    id: str
    owner: str
    conversation_id: str
    title: str
    body: str
    created_at: float
    digest: str
    brands: frozenset[str]
    all_brands: bool


@dataclass(frozen=True)
class DeletePreview:
    reports: tuple[SavedReport, ...]
    challenge: str
    expires_at: float


class ReportStore:
    def __init__(
        self, path: Path, *, ttl: int = 300, clock: Callable[[], float] = time.time
    ) -> None:
        self.ttl, self.clock = ttl, clock
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=2)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("PRAGMA journal_mode=DELETE")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise AgentError(ErrorCode.STORAGE)
        if version == 0:
            self.db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS reports (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, conversation_id TEXT NOT NULL,
                    title TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL,
                    digest TEXT NOT NULL, brands TEXT NOT NULL, all_brands INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS report_owner ON reports(owner, conversation_id);
                CREATE TABLE IF NOT EXISTS challenges (
                    token_hash TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    policy TEXT NOT NULL, snapshot TEXT NOT NULL, expires_at REAL NOT NULL
                );
                PRAGMA user_version=1;
                COMMIT;
            """)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            self.db.execute("BEGIN IMMEDIATE")
            yield
            self.db.execute("COMMIT")
        except Exception as error:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            if isinstance(error, sqlite3.Error):
                raise AgentError(ErrorCode.STORAGE) from None
            raise

    @staticmethod
    def _visible(row: sqlite3.Row, principal: Principal) -> bool:
        return bool(
            row["owner"] == principal.subject
            and (
                principal.all_brands
                or (
                    not row["all_brands"]
                    and frozenset(json.loads(row["brands"])) <= principal.brands
                )
            )
        )

    @staticmethod
    def _report(row: sqlite3.Row) -> SavedReport:
        return SavedReport(
            row["id"],
            row["owner"],
            row["conversation_id"],
            row["title"],
            row["body"],
            row["created_at"],
            row["digest"],
            frozenset(json.loads(row["brands"])),
            bool(row["all_brands"]),
        )

    def save(self, principal: Principal, conversation_id: str, report: Report) -> SavedReport:
        principal.require("reports:write")
        body = sanitize_text(report.model_dump_json())
        title = sanitize_text(report.title)
        report_id = uuid4().hex
        digest = hashlib.sha256(body.encode()).hexdigest()
        created = self.clock()
        with self.transaction():
            self.db.execute(
                "INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    report_id,
                    principal.subject,
                    conversation_id,
                    title,
                    body,
                    created,
                    digest,
                    json.dumps(sorted(principal.brands)),
                    int(principal.all_brands),
                ),
            )
        return SavedReport(
            report_id,
            principal.subject,
            conversation_id,
            title,
            body,
            created,
            digest,
            principal.brands,
            principal.all_brands,
        )

    def list(
        self,
        principal: Principal,
        *,
        mention: str = "",
        conversation_id: str | None = None,
    ) -> tuple[SavedReport, ...]:
        principal.require("reports:read")
        try:
            cursor = self.db.execute(
                "SELECT * FROM reports WHERE owner=? "
                "AND instr(lower(title || ' ' || body), lower(?)) > 0 "
                "AND (? IS NULL OR conversation_id=?) ORDER BY created_at, id",
                (principal.subject, mention, conversation_id, conversation_id),
            )
            found = []
            for row in cursor:
                if self._visible(row, principal):
                    found.append(self._report(row))
                if len(found) > 100:
                    raise AgentError(ErrorCode.BUDGET)
            return tuple(found)
        except sqlite3.Error:
            raise AgentError(ErrorCode.STORAGE) from None

    def preview_delete(
        self,
        principal: Principal,
        *,
        mention: str = "",
        conversation_id: str | None = None,
    ) -> DeletePreview:
        principal.require("reports:delete")
        with self.transaction():
            selected = self.list(principal, mention=mention, conversation_id=conversation_id)
            if not selected:
                return DeletePreview((), "", self.clock())
            challenge = secrets.token_urlsafe(24)
            expires = self.clock() + self.ttl
            snapshot = json.dumps(
                [
                    {
                        "id": r.id,
                        "digest": r.digest,
                        "title": r.title,
                        "conversation_id": r.conversation_id,
                        "brands": sorted(r.brands),
                        "all_brands": r.all_brands,
                    }
                    for r in selected
                ],
                sort_keys=True,
            )
            self.db.execute("DELETE FROM challenges WHERE expires_at <= ?", (self.clock(),))
            self.db.execute(
                "INSERT INTO challenges VALUES (?, ?, ?, ?, ?)",
                (
                    hashlib.sha256(challenge.encode()).hexdigest(),
                    principal.subject,
                    principal.fingerprint,
                    snapshot,
                    expires,
                ),
            )
        return DeletePreview(selected, challenge, expires)

    def get(self, principal: Principal, report_id: str) -> SavedReport:
        principal.require("reports:read")
        try:
            row = self.db.execute(
                "SELECT * FROM reports WHERE id=? AND owner=?", (report_id, principal.subject)
            ).fetchone()
        except sqlite3.Error:
            raise AgentError(ErrorCode.STORAGE) from None
        if row is None or not self._visible(row, principal):
            raise AgentError(ErrorCode.ACCESS)
        return self._report(row)

    def confirm_delete(self, confirmation: str, authenticate: Callable[[], Principal]) -> int:
        if not confirmation.startswith("DELETE "):
            raise AgentError(ErrorCode.CONFIRMATION)
        token = confirmation.removeprefix("DELETE ")
        if not 20 <= len(token) <= 100 or token.strip() != token:
            raise AgentError(ErrorCode.CONFIRMATION)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.transaction():
            principal = authenticate()
            principal.require("reports:delete")
            principal.require("reports:read")
            challenge = self.db.execute(
                "SELECT * FROM challenges WHERE token_hash=?", (token_hash,)
            ).fetchone()
            if (
                challenge is None
                or challenge["owner"] != principal.subject
                or challenge["policy"] != principal.fingerprint
                or challenge["expires_at"] <= self.clock()
            ):
                raise AgentError(ErrorCode.CONFIRMATION)
            snapshot = json.loads(challenge["snapshot"])
            for expected in snapshot:
                row = self.db.execute(
                    "SELECT * FROM reports WHERE id=? AND owner=?",
                    (expected["id"], principal.subject),
                ).fetchone()
                if row is None or not self._visible(row, principal):
                    raise AgentError(ErrorCode.CONFIRMATION)
                actual_digest = hashlib.sha256(row["body"].encode()).hexdigest()
                if (
                    actual_digest != expected["digest"]
                    or row["digest"] != expected["digest"]
                    or row["title"] != expected["title"]
                    or row["conversation_id"] != expected["conversation_id"]
                    or sorted(json.loads(row["brands"])) != expected["brands"]
                    or bool(row["all_brands"]) != expected["all_brands"]
                ):
                    raise AgentError(ErrorCode.CONFIRMATION)
            if (
                authenticate().fingerprint != principal.fingerprint
                or challenge["expires_at"] <= self.clock()
            ):
                raise AgentError(ErrorCode.CONFIRMATION)
            for expected in snapshot:
                self.db.execute(
                    "DELETE FROM reports WHERE id=? AND owner=?",
                    (expected["id"], principal.subject),
                )
            self.db.execute("DELETE FROM challenges WHERE token_hash=?", (token_hash,))
        return len(snapshot)

    def cancel_delete(self, challenge: str, principal: Principal) -> None:
        principal.require("reports:delete")
        with self.transaction():
            self.db.execute(
                "DELETE FROM challenges WHERE token_hash=? AND owner=?",
                (hashlib.sha256(challenge.encode()).hexdigest(), principal.subject),
            )

    def close(self) -> None:
        self.db.close()
