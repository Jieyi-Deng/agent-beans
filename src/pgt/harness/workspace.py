"""One SQLite workspace for the deterministic collaboration harness."""

import json
import sqlite3
from pathlib import Path

from pgt.harness.approvals import Approvals
from pgt.harness.artifacts import ArtifactStore
from pgt.harness.events import EventLog
from pgt.harness.facts import FactStore
from pgt.harness.ledger import EvidenceLedger
from pgt.harness.requests import RequestStore


class Workspace:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        if str(path) != ":memory:":  # someone reading the file never stops the team writing
            self._conn.execute("PRAGMA journal_mode=WAL")
        self.events = EventLog(self._conn)
        self.artifacts = ArtifactStore(self.events)
        self.facts = FactStore(self.events, self.artifacts)
        self.ledger = EvidenceLedger(self.events)
        self.approvals = Approvals(self.events, self.artifacts)
        self.requests = RequestStore(self.events)
        with self.events._transaction():  # memory reads filter the log by event type
            self._conn.execute("CREATE INDEX IF NOT EXISTS events_type ON events (type, id)")
            self._conn.execute("CREATE TABLE IF NOT EXISTS sandbox "
                               "(id INTEGER PRIMARY KEY CHECK (id = 1), content TEXT NOT NULL)")

    def save_sandbox(self, data: dict) -> None:
        """Keep the sandbox ad account's state (`SandboxAccount.dump()`), replacing the last
        one, so a restarted server carries on where it was. Never a real account's."""
        with self.events._transaction():
            self._conn.execute(
                "INSERT INTO sandbox (id, content) VALUES (1, ?) "
                "ON CONFLICT (id) DO UPDATE SET content = excluded.content", (json.dumps(data),))

    def saved_sandbox(self) -> dict | None:
        """The sandbox account's state as last saved, or None (a workspace from before the
        sandbox was saved, or one that never had it)."""
        row = self._conn.execute("SELECT content FROM sandbox WHERE id = 1").fetchone()
        return json.loads(row[0]) if row else None

    def close(self) -> None:
        self._conn.close()
