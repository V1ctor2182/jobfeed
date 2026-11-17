"""The shared account password goes into the pipeline and never comes back out."""

import json
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from jobfeed.account_password import AccountPasswordError, AccountPasswordService, AccountPasswordState, AccountPasswordUpdate
from jobfeed.web.errors import install_error_handling
from jobfeed.web.routes.account_password import router

ORIGIN = "http://127.0.0.1:8765"
SECRET = "Tr1cky-pass!"


class FakeService:
    def __init__(self):
        self.saved = []
        self.failure = None

    def state(self):
        return AccountPasswordState(password_set=bool(self.saved), accounts=9)

    def save(self, update):
        if self.failure:
            raise self.failure
        self.saved.append(update.password)
        return self.state()


def client(service):
    application = FastAPI()
    install_error_handling(application)
    application.state.account_password_service = service
    application.include_router(router, prefix="/api")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url=ORIGIN)


async def test_the_status_says_only_whether_a_password_is_set():
    async with client(FakeService()) as http:
        result = await http.get("/api/my-profile/account-password")
    assert result.json() == {"password_set": False, "accounts": 9}
    assert result.headers["cache-control"] == "no-store"


async def test_only_the_dashboards_own_origin_can_save_and_the_reply_never_carries_the_password():
    service = FakeService()
    async with client(service) as http:
        for headers in ({}, {"Origin": "https://attacker.test"}, {"Origin": "http://localhost:8765"}):
            assert (await http.put("/api/my-profile/account-password", json={"password": SECRET}, headers=headers)).status_code == 403
        assert service.saved == []
        saved = await http.put("/api/my-profile/account-password", json={"password": SECRET}, headers={"Origin": ORIGIN})
    assert (saved.status_code, saved.json()) == (200, {"password_set": True, "accounts": 9})
    assert SECRET not in saved.text
    assert service.saved == [SECRET]


async def test_a_refused_password_reports_the_rule_and_not_the_password():
    service = FakeService()
    service.failure = AccountPasswordError("invalid", "The password was not saved: it needs a digit.")
    async with client(service) as http:
        result = await http.put("/api/my-profile/account-password", json={"password": "NoDigits!!"}, headers={"Origin": ORIGIN})
    assert (result.status_code, result.json()["error"]["code"]) == (422, "account_password_invalid")
    assert "NoDigits" not in result.text


def test_the_adapter_sends_the_password_on_standard_input_never_as_an_argument(monkeypatch, tmp_path: Path):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, json.dumps({"password_set": True, "accounts": [{}, {}]}), "")

    monkeypatch.setattr(subprocess, "run", run)
    state = AccountPasswordService(tmp_path).save(AccountPasswordUpdate(password=SECRET))
    assert state == AccountPasswordState(password_set=True, accounts=2)
    save_args, save_kwargs = calls[0]
    assert save_args[-2:] == ["accounts", "save-password"]
    assert save_kwargs["input"] == SECRET
    assert all(SECRET not in str(argument) for call in calls for argument in call[0])


def test_a_refusal_from_the_pipeline_is_an_invalid_password(monkeypatch, tmp_path: Path):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 2, "", json.dumps({"error": "The password was not saved: it needs a digit."}))

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(AccountPasswordError) as caught:
        AccountPasswordService(tmp_path).save(AccountPasswordUpdate(password="NoDigits!!"))
    assert (caught.value.code, str(caught.value)) == ("invalid", "The password was not saved: it needs a digit.")
