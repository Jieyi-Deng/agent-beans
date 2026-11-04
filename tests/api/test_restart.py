"""A server restarted on an existing workspace carries on where it was: the sandbox ad account,
the one part of the team held in memory, is saved in the workspace and loaded back."""

from fastapi.testclient import TestClient

from pgt.api.app import create_app, offline_team
from tests.api.test_app import _idle


def _boot(tmp_path, **kwargs):
    team = offline_team(str(tmp_path / "ws.db"), str(tmp_path / "media"))
    return team, create_app(team, **kwargs)


def _pages(c: TestClient) -> dict:
    return {path: c.get(path).json() for path in (
        "/api/dashboard?days=7", "/api/dashboard?days=14", "/api/assets", "/api/deliverables")}


def test_a_restart_keeps_the_seeded_sandbox_and_the_next_day_runs(tmp_path):
    team, app = _boot(tmp_path)
    with TestClient(app) as c:
        _idle(c)
        before, dump = _pages(c), team.account.dump()
        runs = len(team.ws.events.list("loop.started"))
    assert dump["changes"], "the seeded week should include Operator writes"
    team.ws.close()

    team, app = _boot(tmp_path)
    with TestClient(app) as c:
        assert team.account.dump() == dump  # loaded back, not seeded again
        _idle(c)
        assert len(team.ws.events.list("loop.started")) == runs
        assert _pages(c) == before
        assert before["/api/dashboard?days=7"]["ready"]
        day = c.get("/api/status").json()["day"]
        assert c.post("/api/run/day").status_code == 200
        assert _idle(c)["day"] == day + 1
        assert c.get("/api/dashboard").json()["end"] == day + 1
    team.ws.close()


def test_a_restart_keeps_a_cold_start_sandbox(tmp_path):
    team, app = _boot(tmp_path, seed_days=0)
    with TestClient(app) as c:
        r = c.post("/api/access/connect", json={"kind": "sandbox", "level": 2})
        assert r.status_code == 200, r.text
        _idle(c)
        assert c.post("/api/run/day").status_code == 200
        _idle(c)
        before, dump = _pages(c), team.account.dump()
    team.ws.close()

    team, app = _boot(tmp_path, seed_days=0)
    with TestClient(app) as c:
        assert team.account.dump() == dump and team.account.day == 1
        assert _pages(c) == before
        # Connecting again keeps the account it had, not a fresh one on day 0.
        assert c.post("/api/access/connect", json={"kind": "sandbox", "level": 2}).status_code == 200
        _idle(c)
        assert team.account.day == 1
        assert c.post("/api/run/day").status_code == 200
        assert _idle(c)["day"] == 2
    team.ws.close()


def test_a_workspace_from_before_the_save_still_starts(tmp_path):
    """Workspaces recorded before the sandbox was saved have none to load: the team starts
    without an account and does not seed again, as it did then."""
    team, app = _boot(tmp_path)
    with TestClient(app) as c:
        _idle(c)
    with team.ws.events._transaction():
        team.ws.events._conn.execute("DELETE FROM sandbox")
    team.ws.close()

    team, app = _boot(tmp_path)
    with TestClient(app) as c:
        assert team.account is None and not c.get("/api/status").json()["busy"]
        assert not c.get("/api/dashboard").json()["ready"]
    team.ws.close()
