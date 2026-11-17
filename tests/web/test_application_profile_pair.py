"""Profile proof and automatic pairing use real source and focus sockets."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from jobfeed.application_tabs import ApplicationTabs
from jobfeed.services.jobright_bridge import JobrightBridge
from jobfeed.web.routes.jobright_bridge import router as source_router
from tests.web import test_application_tabs
from tests.web.test_application_tabs import (
    CONFLICT,
    EXTENSION,
    FORBIDDEN,
    OK,
    ORIGIN,
    OWNER,
    TAB_ID,
    UNAVAILABLE,
    URL,
    headers,
    hello,
)

tab_app = test_application_tabs.tab_app

INVALID = 422


@pytest.fixture
def profile_app(tab_app):
    tab_app.state.context = {"jobright_bridge": JobrightBridge()}
    tab_app.include_router(source_router, prefix="/api")
    return tab_app


def source_ready(ws):
    ws.send_json(
        {
            "type": "hello",
            "protocol": 1,
            "sources": ["application-tab-pair-v1"],
        }
    )
    assert ws.receive_json()["type"] == "ready"


def probe(client, source, matches=None):
    candidates = [{"native_tab_id": TAB_ID, "url": URL}]
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(
            client.post,
            "/api/application-tabs/profile-probe",
            json={"candidates": candidates},
            headers=headers(client),
        )
        command = source.receive_json()
        assert command["action"] == "probe"
        source.send_json(
            {
                "type": "application_tab_result",
                "request_id": command["request_id"],
                "browser_session_id": "browser-session",
                "matches": candidates if matches is None else matches,
            }
        )
        return pending.result(timeout=2)


def test_probe_requires_actual_profile_match_and_current_connection(profile_app):
    with TestClient(profile_app, base_url=ORIGIN) as client:
        assert (
            client.get("/api/application-tabs").json()["profile_probe_available"]
            is False
        )
        with client.websocket_connect(
            "/api/sources/jobright/bridge", headers={"Origin": EXTENSION}
        ) as source:
            source_ready(source)
            assert (
                client.get("/api/application-tabs").json()["profile_probe_available"]
                is True
            )
            assert probe(client, source, []).status_code == CONFLICT
            assert (
                probe(
                    client, source, [{"native_tab_id": TAB_ID, "url": URL + "/wrong"}]
                ).status_code
                == UNAVAILABLE
            )
            proof = probe(client, source).json()
            body = {"profile_proof": proof["profile_proof"]}
            assert (
                client.post(
                    "/api/application-tabs/profile-validate",
                    json=body,
                    headers=headers(client),
                ).json()["valid"]
                is True
            )
        with client.websocket_connect(
            "/api/sources/jobright/bridge", headers={"Origin": EXTENSION}
        ) as replacement:
            source_ready(replacement)
            assert (
                client.post(
                    "/api/application-tabs/profile-validate",
                    json=body,
                    headers=headers(client),
                ).status_code
                == CONFLICT
            )


def test_auto_pair_waits_for_real_focus_socket_and_rechecks_owner(profile_app):
    with (
        TestClient(profile_app, base_url=ORIGIN) as client,
        client.websocket_connect(
            "/api/sources/jobright/bridge", headers={"Origin": EXTENSION}
        ) as source,
    ):
        source_ready(source)
        proof = probe(client, source).json()
        body = {
            "owner_thread_id": OWNER,
            "profile_proof": proof["profile_proof"],
            "native_tab_id": TAB_ID,
            "url": URL,
        }
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(
                client.post,
                "/api/application-tabs/1/auto-connect",
                json=body,
                headers=headers(client),
            )
            command = source.receive_json()
            assert command["action"] == "connect"
            assert command["native_tab_id"] == TAB_ID
            connected = client.post(
                "/api/application-tabs/connect",
                json={
                    "code": command["code"],
                    "browser_session_id": "browser-session",
                    "native_tab_id": TAB_ID,
                    "url": URL,
                },
                headers={"Origin": EXTENSION},
            )
            assert connected.status_code == OK
            credentials = connected.json()
            assert not pending.done()
            with client.websocket_connect(
                "/api/application-tabs/bridge", headers={"Origin": EXTENSION}
            ) as focus:
                focus.send_json(hello(credentials))
                assert focus.receive_json()["type"] == "ready"
                source.send_json(
                    {
                        "type": "application_tab_result",
                        "request_id": command["request_id"],
                        "binding_id": credentials["binding_id"],
                        "connected": True,
                    }
                )
                result = pending.result(timeout=2)
                assert result.status_code == OK, result.text
                assert result.json()["state"] == "connected"
        with sqlite3.connect(profile_app.state.application_tabs.db_path) as db:
            db.execute(
                "UPDATE application_queue SET codex_application_session_id='new-owner'"
            )
        assert (
            client.post(
                "/api/application-tabs/1/auto-connect",
                json=body,
                headers=headers(client),
            ).status_code
            == CONFLICT
        )


def test_false_ready_and_disconnect_never_confirm_pairing(profile_app):
    with (
        TestClient(profile_app, base_url=ORIGIN) as client,
        client.websocket_connect(
            "/api/sources/jobright/bridge", headers={"Origin": EXTENSION}
        ) as source,
    ):
        source_ready(source)
        proof = probe(client, source).json()
        body = {
            "owner_thread_id": OWNER,
            "profile_proof": proof["profile_proof"],
            "native_tab_id": TAB_ID,
            "url": URL,
        }
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(
                client.post,
                "/api/application-tabs/1/auto-connect",
                json=body,
                headers=headers(client),
            )
            command = source.receive_json()
            source.send_json(
                {
                    "type": "application_tab_result",
                    "request_id": command["request_id"],
                    "binding_id": "unknown",
                    "connected": True,
                }
            )
            assert pending.result(timeout=2).status_code == UNAVAILABLE
            pending = pool.submit(
                client.post,
                "/api/application-tabs/1/auto-connect",
                json=body,
                headers=headers(client),
            )
            source.receive_json()
            source.close()
            assert pending.result(timeout=2).status_code == UNAVAILABLE
            assert not profile_app.state.application_tabs.connecting_jobs


def test_auto_pair_rejects_changed_url_and_other_profile_session(profile_app):
    with (
        TestClient(profile_app, base_url=ORIGIN) as client,
        client.websocket_connect(
            "/api/sources/jobright/bridge", headers={"Origin": EXTENSION}
        ) as source,
    ):
        source_ready(source)
        proof = probe(client, source).json()
        body = {
            "owner_thread_id": OWNER,
            "profile_proof": proof["profile_proof"],
            "native_tab_id": TAB_ID,
            "url": URL,
        }
        for change in [
            {"url": URL + "/other"},
            {"browser_session_id": "wrong-profile"},
        ]:
            with ThreadPoolExecutor() as pool:
                pending = pool.submit(
                    client.post,
                    "/api/application-tabs/1/auto-connect",
                    json=body,
                    headers=headers(client),
                )
                command = source.receive_json()
                connected = client.post(
                    "/api/application-tabs/connect",
                    json={
                        "code": command["code"],
                        "browser_session_id": "browser-session",
                        "native_tab_id": TAB_ID,
                        "url": URL,
                        **change,
                    },
                    headers={"Origin": EXTENSION},
                )
                assert connected.status_code == CONFLICT
                source.send_json(
                    {
                        "type": "application_tab_result",
                        "request_id": command["request_id"],
                        "error": "Tab mismatch",
                    }
                )
                assert pending.result(timeout=2).status_code == UNAVAILABLE


def test_expired_proof_old_extension_bounds_and_csrf(profile_app):
    with TestClient(profile_app, base_url=ORIGIN) as client:
        candidates = [{"native_tab_id": TAB_ID, "url": URL}]
        assert (
            client.post(
                "/api/application-tabs/profile-probe", json={"candidates": candidates}
            ).status_code
            == FORBIDDEN
        )
        assert (
            client.post(
                "/api/application-tabs/profile-probe",
                json={"candidates": candidates * 17},
                headers=headers(client),
            ).status_code
            == INVALID
        )
        with client.websocket_connect(
            "/api/sources/jobright/bridge", headers={"Origin": EXTENSION}
        ) as source:
            source_ready(source)
            proof = probe(client, source).json()
            service = profile_app.state.application_tabs
            service.profile_proofs[proof["profile_proof"]].expires_at -= timedelta(
                minutes=6
            )
            assert (
                client.post(
                    "/api/application-tabs/profile-validate",
                    json={"profile_proof": proof["profile_proof"]},
                    headers=headers(client),
                ).status_code
                == CONFLICT
            )
        with client.websocket_connect(
            "/api/sources/jobright/bridge", headers={"Origin": EXTENSION}
        ) as old:
            old.send_json({"type": "hello", "protocol": 1, "sources": ["jobright"]})
            old.receive_json()
            assert (
                "Reload"
                in client.get("/api/application-tabs").json()["profile_probe_reason"]
            )
            result = client.post(
                "/api/application-tabs/profile-probe",
                json={"candidates": candidates},
                headers=headers(client),
            )
            assert result.status_code == UNAVAILABLE
            assert "Reload" in result.text


def register_form(client, source, profile_app):
    """Establish a durable relationship through the actual approved pairing route."""
    proof = probe(client, source).json()
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(
            client.post,
            "/api/application-tabs/1/auto-connect",
            json={
                "owner_thread_id": OWNER,
                "profile_proof": proof["profile_proof"],
                "native_tab_id": TAB_ID,
                "url": URL,
            },
            headers=headers(client),
        )
        command = source.receive_json()
        assert command["action"] == "connect"
        credentials = client.post(
            "/api/application-tabs/connect",
            json={
                "code": command["code"],
                "browser_session_id": "browser-session",
                "native_tab_id": TAB_ID,
                "url": URL,
            },
            headers={"Origin": EXTENSION},
        ).json()
        with client.websocket_connect(
            "/api/application-tabs/bridge", headers={"Origin": EXTENSION}
        ) as focus:
            focus.send_json(hello(credentials))
            assert focus.receive_json()["type"] == "ready"
            source.send_json(
                {
                    "type": "application_tab_result",
                    "request_id": command["request_id"],
                    "binding_id": credentials["binding_id"],
                    "connected": True,
                }
            )
            assert pending.result(timeout=2).status_code == OK
    old = profile_app.state.application_tabs
    profile_app.state.application_tabs = ApplicationTabs(old.root, old.db_path)


@pytest.mark.parametrize("change_owner", [False, True])
@pytest.mark.parametrize(
    "completion", [None, "submission-closeouts", "closed-jobs", "terminal"]
)
def test_open_existing_recovers_registration_after_restart_then_waits_for_receipt(
    profile_app, change_owner, completion
):
    with (
        TestClient(profile_app, base_url=ORIGIN) as client,
        client.websocket_connect(
            "/api/sources/jobright/bridge", headers={"Origin": EXTENSION}
        ) as source,
    ):
        source_ready(source)
        register_form(client, source, profile_app)
        if completion:
            tabs = profile_app.state.application_tabs
            with sqlite3.connect(tabs.db_path) as db:
                db.execute("DELETE FROM application_queue")
            if completion == "terminal":
                with sqlite3.connect(tabs.db_path) as db:
                    db.execute(
                        "CREATE TABLE application_terminal("
                        "job_id INTEGER,codex_application_session_id TEXT)"
                    )
                    db.execute("INSERT INTO application_terminal VALUES(1,?)", (OWNER,))
            else:
                pointer = tabs.root / ".pipeline" / completion / "1.json"
                pointer.parent.mkdir()
                pointer.write_text(json.dumps({"job_id": 1, "thread_id": OWNER}))
            with sqlite3.connect(
                tabs.root / ".pipeline" / "coordination.sqlite3"
            ) as db:
                db.execute("UPDATE coordination_tabs SET state='preserved'")
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(
                client.post,
                "/api/application-tabs/1/open-existing",
                json={"owner_thread_id": OWNER},
                headers=headers(client),
            )
            actual_url = URL
            probe_command = source.receive_json()
            assert probe_command["action"] == "probe"
            assert probe_command["candidates"] == [
                {"native_tab_id": TAB_ID, "url": actual_url}
            ]
            source.send_json(
                {
                    "type": "application_tab_result",
                    "request_id": probe_command["request_id"],
                    "browser_session_id": "browser-session",
                    "matches": probe_command["candidates"],
                }
            )
            command = source.receive_json()
            assert command["action"] == "connect"
            assert command["url"] == actual_url
            credentials = client.post(
                "/api/application-tabs/connect",
                json={
                    "code": command["code"],
                    "browser_session_id": "browser-session",
                    "native_tab_id": TAB_ID,
                    "url": actual_url,
                },
                headers={"Origin": EXTENSION},
            ).json()
            with client.websocket_connect(
                "/api/application-tabs/bridge", headers={"Origin": EXTENSION}
            ) as focus:
                focus.send_json(hello(credentials))
                assert focus.receive_json()["type"] == "ready"
                source.send_json(
                    {
                        "type": "application_tab_result",
                        "request_id": command["request_id"],
                        "binding_id": credentials["binding_id"],
                        "connected": True,
                    }
                )
                focus_command = focus.receive_json()
                assert focus_command["type"] == "focus"
                assert not pending.done()
                if change_owner:
                    with sqlite3.connect(
                        profile_app.state.application_tabs.db_path
                    ) as db:
                        db.execute("DELETE FROM application_queue")
                        db.execute(
                            "INSERT INTO application_queue VALUES(1,'replacement')"
                        )
                focus.send_json(
                    {
                        "type": "focus_result",
                        "request_id": focus_command["request_id"],
                        "binding_id": credentials["binding_id"],
                        "status": "focused",
                    }
                )
                result = pending.result(timeout=2)
                assert result.status_code == (CONFLICT if change_owner else OK), (
                    result.text
                )
                if not change_owner:
                    assert result.json() == {"status": "focused"}
                    again = pool.submit(
                        client.post,
                        "/api/application-tabs/1/open-existing",
                        json={"owner_thread_id": OWNER},
                        headers=headers(client),
                    )
                    next_focus = focus.receive_json()
                    assert next_focus["binding_id"] == credentials["binding_id"]
                    focus.send_json(
                        {
                            "type": "focus_result",
                            "request_id": next_focus["request_id"],
                            "binding_id": credentials["binding_id"],
                            "status": "focused",
                        }
                    )
                    assert again.result(timeout=2).json() == {"status": "focused"}


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "foreign-id",
        "foreign-origin",
        "duplicate",
        "owner-changed",
        "session-changed",
    ],
)
def test_open_existing_refuses_stale_or_different_profile_registration(
    profile_app, case
):
    with (
        TestClient(profile_app, base_url=ORIGIN) as client,
        client.websocket_connect(
            "/api/sources/jobright/bridge", headers={"Origin": EXTENSION}
        ) as source,
    ):
        source_ready(source)
        register_form(client, source, profile_app)
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(
                client.post,
                "/api/application-tabs/1/open-existing",
                json={"owner_thread_id": OWNER},
                headers=headers(client),
            )
            command = source.receive_json()
            matches = [{"native_tab_id": TAB_ID, "url": URL}]
            if case == "missing":
                matches = []
            elif case == "foreign-id":
                matches[0]["native_tab_id"] = TAB_ID + 1
            elif case == "foreign-origin":
                matches[0]["url"] = "https://other.example/form"
            elif case == "duplicate":
                matches *= 2
            elif case == "owner-changed":
                with sqlite3.connect(profile_app.state.application_tabs.db_path) as db:
                    db.execute(
                        "UPDATE application_queue "
                        "SET codex_application_session_id='replacement'"
                    )
            source.send_json(
                {
                    "type": "application_tab_result",
                    "request_id": command["request_id"],
                    "browser_session_id": "other-session"
                    if case == "session-changed"
                    else "browser-session",
                    "matches": matches,
                }
            )
            assert pending.result(timeout=2).status_code in {CONFLICT, UNAVAILABLE}
            assert profile_app.state.application_tabs.bindings == {}
            assert profile_app.state.application_tabs.pairings == {}


def test_open_existing_requires_csrf_owner_and_prior_registration(profile_app):
    with TestClient(profile_app, base_url=ORIGIN) as client:
        endpoint = "/api/application-tabs/1/open-existing"
        assert (
            client.post(endpoint, json={"owner_thread_id": OWNER}).status_code
            == FORBIDDEN
        )
        assert (
            client.post(
                endpoint, json={"owner_thread_id": "wrong"}, headers=headers(client)
            ).status_code
            == CONFLICT
        )
        with client.websocket_connect(
            "/api/sources/jobright/bridge", headers={"Origin": EXTENSION}
        ) as source:
            source_ready(source)
            result = client.post(
                endpoint, json={"owner_thread_id": OWNER}, headers=headers(client)
            )
            assert result.status_code == CONFLICT
            assert "has not connected" in result.text


def test_unrelated_profile_proof_cannot_register_application_form(profile_app):
    with (
        TestClient(profile_app, base_url=ORIGIN) as client,
        client.websocket_connect(
            "/api/sources/jobright/bridge", headers={"Origin": EXTENSION}
        ) as source,
    ):
        source_ready(source)
        proof = probe(client, source).json()
        response = client.post(
            "/api/application-tabs/1/auto-connect",
            json={
                "owner_thread_id": OWNER,
                "profile_proof": proof["profile_proof"],
                "native_tab_id": TAB_ID,
                "url": URL + "/other",
            },
            headers=headers(client),
        )
        assert response.status_code == CONFLICT
        assert "selected_tab_not_in_profile_proof" in response.text
        tabs = profile_app.state.application_tabs
        assert not (tabs.root / ".pipeline" / "application-tabs.sqlite3").exists()
        assert not tabs.bindings
        assert not tabs.pairings


@pytest.mark.parametrize(
    "column,value",
    [
        ("browser_id", "other-browser"),
        ("record_id", "other-record"),
        ("owner", "other-owner"),
        ("scope", "other-scope"),
        ("tab_id", str(TAB_ID + 1)),
    ],
)
def test_reopen_requires_exact_registered_owner_and_coordination_target(
    profile_app, column, value
):
    with (
        TestClient(profile_app, base_url=ORIGIN) as client,
        client.websocket_connect(
            "/api/sources/jobright/bridge", headers={"Origin": EXTENSION}
        ) as source,
    ):
        source_ready(source)
        register_form(client, source, profile_app)
        tabs = profile_app.state.application_tabs
        with sqlite3.connect(
            tabs.root / ".pipeline" / "application-tabs.sqlite3"
        ) as db:
            # Column names come only from this fixed synthetic parameter list.
            db.execute(f"UPDATE registered_tabs SET {column}=?", (value,))
        response = client.post(
            "/api/application-tabs/1/open-existing",
            json={"owner_thread_id": OWNER},
            headers=headers(client),
        )
        assert response.status_code == CONFLICT
        assert not tabs.bindings
        assert not tabs.pairings


def test_completed_owner_conflict_and_registration_symlink_fail_closed(profile_app):
    tabs = profile_app.state.application_tabs
    with sqlite3.connect(tabs.db_path) as db:
        db.execute("DELETE FROM application_queue")
    for directory, owner in [
        ("submission-closeouts", OWNER),
        ("closed-jobs", "new-owner"),
    ]:
        pointer = tabs.root / ".pipeline" / directory / "1.json"
        pointer.parent.mkdir()
        pointer.write_text(json.dumps({"job_id": 1, "thread_id": owner}))
    with TestClient(profile_app, base_url=ORIGIN) as client:
        response = client.post(
            "/api/application-tabs/1/open-existing",
            json={"owner_thread_id": OWNER},
            headers=headers(client),
        )
        assert response.status_code == CONFLICT
        assert "owner_changed" in response.text
        with sqlite3.connect(tabs.db_path) as db:
            db.execute("INSERT INTO application_queue VALUES(1,?)", (OWNER,))
        outside = tabs.root.parent / "unrelated.sqlite3"
        outside.write_text("untouched")
        (tabs.root / ".pipeline" / "application-tabs.sqlite3").symlink_to(outside)
        response = client.post(
            "/api/application-tabs/1/open-existing",
            json={"owner_thread_id": OWNER},
            headers=headers(client),
        )
        assert response.status_code == UNAVAILABLE
        assert outside.read_text() == "untouched"
        assert not tabs.bindings
