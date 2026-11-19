"""Profile edits preserve private source material and never dispatch applications."""

from __future__ import annotations

import json
import stat
from importlib import import_module
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from jobfeed.my_profile import MyProfileService
from jobfeed.my_profile_document import PersonalProfileStore, ProfileConflict
from jobfeed.my_profile_models import PersonalDetails
from jobfeed.onboarding_resume_types import ResumeDraftState
from jobfeed.web.errors import install_error_handling
from jobfeed.web.routes.my_profile import router

ORIGIN = "http://127.0.0.1:8765"
HTTP_OK = 200
HTTP_CONFLICT = 409
HTTP_FORBIDDEN = 403
HTTP_INVALID = 422
PRIVATE_MODE = 0o600
PRESERVED = (
    "\n## Accounts\n- Password: confidential-test-value\n"
    "  private continuation\n\n## Confirmed user answer — job 9\n"
    "Scope: job:9\n- Relocation: Only for this company\n"
    "\n## Personal notes\nA user-authored paragraph to preserve.\n"
)


def _service(tmp_path: Path) -> MyProfileService:
    pipeline = tmp_path / "pipeline"
    pipeline.mkdir()
    (pipeline / "application-profile.md").write_text(
        "# Application profile\n\n## Identity and contact\n"
        "- Legal name: Test Candidate\n- Primary email: candidate@example.test\n"
        "- Phone: 555-0100\n" + PRESERVED
    )
    upload = tmp_path / "jobfeed/data/resumes/candidate.pdf"
    upload.parent.mkdir(parents=True)
    upload.write_bytes(b"%PDF-1.4\nsynthetic fixture\n%%EOF")
    draft = ResumeDraftState(
        stored_name=upload.name, original_name="candidate.pdf", extracted_text="Test"
    )
    return MyProfileService(
        root=tmp_path / "jobfeed", pipeline_root=pipeline, resume_state=lambda: draft
    )


def _app(service: MyProfileService) -> FastAPI:
    app = FastAPI()
    install_error_handling(app)
    app.state.my_profile_service = service
    app.include_router(router, prefix="/api")
    return app


async def test_http_save_preserves_scoped_material_and_hides_credentials(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(service)), base_url=ORIGIN
    ) as client:
        before = await client.get("/api/my-profile")
        assert before.status_code == HTTP_OK
        assert before.headers["cache-control"] == "no-store"
        assert "confidential" not in before.text
        assert str(tmp_path) not in before.text
        body = before.json()
        assert body["reviewed"] is False
        body["personal_details"]["preferred_name"] = "Test"
        body["personal_details"]["relocation"] = "Ask for each role"
        saved = await client.put(
            "/api/my-profile/personal",
            headers={"Origin": ORIGIN},
            json={
                "revision": body["revision"],
                "personal_details": body["personal_details"],
            },
        )
        assert saved.status_code == HTTP_OK, saved.text
        assert saved.json()["reviewed"] is True
        stale = await client.put(
            "/api/my-profile/personal",
            headers={"Origin": ORIGIN},
            json={
                "revision": body["revision"],
                "personal_details": body["personal_details"],
            },
        )
        assert stale.status_code == HTTP_CONFLICT
    content = service.personal.profile.read_text()
    assert PRESERVED in content
    assert "- Relocation: Ask for each role\n" in content
    assert "- Preferred name: Test\n" in content
    assert stat.S_IMODE(service.personal.profile.stat().st_mode) == PRIVATE_MODE
    assert not (service.pipeline_root / ".pipeline").exists()


async def test_profile_mutations_reject_cross_origin_and_markdown_injection(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    original = service.personal.profile.read_bytes()
    state = service.state()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(service)), base_url=ORIGIN
    ) as client:
        body = {
            "revision": state.revision,
            "personal_details": state.personal_details.model_dump(),
        }
        denied = await client.put(
            "/api/my-profile/personal",
            headers={"Origin": "https://example.test"},
            json=body,
        )
        assert denied.status_code == HTTP_FORBIDDEN
        body["personal_details"]["legal_name"] = "Name\n## Accounts"
        invalid = await client.put(
            "/api/my-profile/personal", headers={"Origin": ORIGIN}, json=body
        )
        assert invalid.status_code == HTTP_INVALID
    assert service.personal.profile.read_bytes() == original


def test_review_survives_scoped_append_but_invalidates_changed_contact(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    initial = service.personal.state()
    service.personal.save(initial.personal_details, initial.revision)
    with service.personal.profile.open("a") as stream:
        stream.write(
            "\n## Confirmed user answer — job 10\nScope: job:10\nAnswer: Yes\n"
        )
    assert service.personal.state().reviewed
    source = service.personal.profile.read_text().replace(
        "- Phone: 555-0100", "- Phone: 555-0101"
    )
    service.personal.profile.write_text(source)
    assert not service.personal.state().reviewed


def test_duplicate_personal_fields_fail_without_exposing_values(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.personal.profile.write_text(
        "## Identity and contact\n- Phone: 1\n- Phone: 2\n"
    )
    with pytest.raises(ProfileConflict, match="repeated personal fields"):
        service.personal.state()


def test_a_learned_fact_survives_loading_and_saving_byte_for_byte(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    assert service.personal.remember("Full name", "Alex Rivera")
    assert not service.personal.remember("full name", "Someone Else")
    before = service.personal.profile.read_text()
    assert "- Full name: Alex Rivera\n" in before
    state = service.personal.state()
    assert state.personal_details.legal_name == "Test Candidate"
    edited = state.personal_details.model_copy(update={"phone": "555-0199"})
    service.personal.save(edited, state.revision)
    after = service.personal.profile.read_text()
    assert after == before.replace("- Phone: 555-0100", "- Phone: 555-0199")
    assert PRESERVED in after


def test_creation_keeps_unknown_values_blank(tmp_path: Path) -> None:
    store = PersonalProfileStore(tmp_path / "profile.md", tmp_path / "review.json")
    assert store.state().revision == "missing"
    result = store.save(PersonalDetails(legal_name="Test Person"), "missing")
    assert result.reviewed
    assert result.personal_details.work_authorization == ""
    assert "Work authorization" not in store.profile.read_text()


def test_application_pdf_addition_is_idempotent_private_and_preserves_entries(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    prior = {
        "id": "original",
        "label": "Original",
        "pdf": "original.pdf",
        "available": False,
        "custom_note": "preserved",
    }
    service.catalog_path.write_text(
        json.dumps({"schema_version": 1, "resumes": [prior]})
    )
    first = service.add_current_resume()
    second = service.add_current_resume()
    assert first.application_resumes == second.application_resumes
    catalog = json.loads(service.catalog_path.read_text())
    assert catalog["resumes"][0] == prior
    added = catalog["resumes"][1]
    copied = service.pipeline_root / added["pdf"]
    assert copied.read_bytes() == b"%PDF-1.4\nsynthetic fixture\n%%EOF"
    assert stat.S_IMODE(copied.stat().st_mode) == PRIVATE_MODE
    assert stat.S_IMODE(service.catalog_path.stat().st_mode) == PRIVATE_MODE
    service.set_available(added["id"], False)
    assert service.state().application_resumes[1].available is False
    assert copied.exists()
    assert copied == (service.root / "data/resumes/candidate.pdf")
    assert not (service.pipeline_root / ".pipeline").exists()


async def test_catalog_routes_accept_only_current_upload_and_boolean_choice(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(service)),
        base_url=ORIGIN,
        headers={"Origin": ORIGIN},
    ) as client:
        invalid = await client.post(
            "/api/my-profile/application-resume",
            json={"path": "/private/elsewhere.pdf"},
        )
        assert invalid.status_code == HTTP_INVALID
        added = await client.post("/api/my-profile/application-resume", json={})
        assert added.status_code == HTTP_OK, added.text
        assert str(tmp_path) not in added.text
        identifier = added.json()["application_resumes"][0]["id"]
        invalid_toggle = await client.put(
            f"/api/my-profile/application-resumes/{identifier}",
            json={"available": "yes"},
        )
        assert invalid_toggle.status_code == HTTP_INVALID
        disabled = await client.put(
            f"/api/my-profile/application-resumes/{identifier}",
            json={"available": False},
        )
        assert disabled.status_code == HTTP_OK
        assert disabled.json()["application_resumes"][0]["available"] is False


def test_failed_application_pdf_add_does_not_mutate_catalog(tmp_path: Path) -> None:
    service = _service(tmp_path)
    source = service.root / "data/resumes/candidate.pdf"
    source.write_bytes(b"not PDF")
    with pytest.raises(ValueError, match="not a valid PDF"):
        service.add_current_resume()
    assert not service.catalog_path.exists()
    assert not (service.pipeline_root / ".pipeline").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("primary_email", "not-email"),
        ("portfolio_url", "javascript:alert(1)"),
        ("linkedin_url", "https://user:password@example.test"),
        ("github_url", "github.com/no-scheme"),
    ],
)
async def test_nonempty_email_and_url_validation(
    tmp_path: Path, field: str, value: str
) -> None:
    service = _service(tmp_path)
    state = service.state()
    fields = state.personal_details.model_dump()
    fields[field] = value
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(service)), base_url=ORIGIN
    ) as client:
        response = await client.put(
            "/api/my-profile/personal",
            headers={"Origin": ORIGIN},
            json={"personal_details": fields, "revision": state.revision},
        )
    assert response.status_code == HTTP_INVALID
    assert value not in response.text
    expected = "valid email address" if field == "primary_email" else "website URL"
    assert expected in response.json()["error"]["message"]
    assert not service.personal.review.exists()


def test_catalog_is_accepted_by_existing_application_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    service.add_current_resume()
    pipeline = Path(__file__).resolve().parents[3] / "resume-application-pipeline"
    if not pipeline.is_dir():
        pytest.skip("needs resume-application-pipeline as a sibling checkout")
    monkeypatch.syspath_prepend(str(pipeline))
    catalog_module = import_module("pipeline.resume_catalog")
    loaded = catalog_module.ResumeCatalog.load(service.catalog_path)
    assert len(loaded.entries) == 1
    assert loaded.entries[0]["label"] == "candidate.pdf"


def test_blank_field_can_be_filled_again_without_changing_document_structure(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    state = service.personal.state()
    blank = state.personal_details.model_copy(update={"phone": ""})
    cleared = service.personal.save(blank, state.revision)
    restored = cleared.personal_details.model_copy(update={"phone": "555-0102"})
    saved = service.personal.save(restored, cleared.revision)
    assert saved.personal_details.phone == "555-0102"
    assert "- Phone: 555-0102\n" in service.personal.profile.read_text()
    assert PRESERVED in service.personal.profile.read_text()


@pytest.mark.parametrize(
    "dashboard", ["http://jobfeed.localhost:1355", "https://jobfeed.localhost:1355"]
)
async def test_application_resume_can_save_from_local_proxy(
    tmp_path: Path, dashboard: str
) -> None:
    service = _service(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(service)), base_url=dashboard
    ) as client:
        response = await client.post(
            "/api/my-profile/application-resume", headers={"Origin": dashboard}, json={}
        )
    assert response.status_code == HTTP_OK, response.text


@pytest.mark.parametrize(
    "origin",
    [
        "https://example.test",
        "http://127.0.0.1:8765",
        "https://jobfeed.localhost:1356",
        "null",
        "",
    ],
)
async def test_proxy_resume_save_rejects_foreign_origins(
    tmp_path: Path, origin: str
) -> None:
    service = _service(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(service)),
        base_url="https://jobfeed.localhost:1355",
    ) as client:
        response = await client.post(
            "/api/my-profile/application-resume", headers={"Origin": origin}, json={}
        )
    assert response.status_code == HTTP_FORBIDDEN
    assert not (service.pipeline_root / "resume-catalog.json").exists()
