"""Saved form details retain observation provenance and never run applications."""

# ruff: noqa: PLR2004, PLR0913

from __future__ import annotations

import json
import sqlite3

import pytest

from jobfeed.application_forms import read_application_form, read_form_resume
from jobfeed.web.app import build_web_app
from tests.web.test_app_skeleton import fake_context, open_client
from tests.web.test_apply_health import controller_manifest, make_monitor, tree
from tests.web.test_confirmation_evidence import fixture

PDF = b"%PDF-1.7\nsynthetic resume fixture"
FIRST = "2026-10-21T12:00:00+00:00"
SECOND = "2026-10-21T12:01:00+00:00"
THIRD = "2026-10-21T12:02:00+00:00"


def form_fixture(tmp_path):
    database, root, folder = fixture(tmp_path)
    scope = controller_manifest(make_monitor(root))
    receipt = {
        "schema_version": 2,
        "job_id": 1,
        "scope": str(scope),
        "owner": "owner-one",
        "resume_id": "existing",
        "label": "Engineering resume",
        "assets": {"pdf": {"path": "selected.pdf"}},
    }
    (folder / "resume-source.json").write_text(json.dumps(receipt))
    (folder / "selected.pdf").write_bytes(PDF)
    return database, root, folder, scope


def capture(
    scope, fields, *, at=FIRST, owner="owner-one", attempt="attempt-one", page="contact"
):
    return {
        "at": at,
        "pageUrl": f"https://careers.example.test/{page}?token=secret",
        "pageKey": f"https://careers.example.test/{page}|{page}|controls",
        "formSnapshot": {
            "capturedAt": at,
            "coverage": "visible_controls",
            "fields": fields,
            "scope": str(scope),
            "jobId": 1,
            "ownerThreadId": owner,
            "attemptId": attempt,
        },
        "selectedResume": {
            "resumeId": "existing",
            "label": "Engineering resume",
            "filename": "selected.pdf",
            "pdfPath": "selected.pdf",
            "ownerThreadId": owner,
        },
    }


def field(key, value, **extra):
    return {"fieldId": key, "label": key, "value": value, "status": "observed", **extra}


def journal(folder, runs, **extra):
    (folder / "application-answers.json").write_text(
        json.dumps({"runs": runs, **extra})
    )


async def test_form_api_is_read_only_and_returns_typed_fields_and_exact_resume(
    tmp_path, monkeypatch
):
    database, root, folder, scope = form_fixture(tmp_path)
    run = capture(
        scope,
        [
            field("Sponsorship", False),
            field("Years", 0),
            field("Optional", "", status="empty"),
            field("Choices", ["A", "B"]),
        ],
    )
    journal(folder, [run], account={"password": "never-return-this"})
    context = fake_context()
    context["settings"].db.path = database
    monkeypatch.setenv("JOBFEED_APPLY_ROOT", str(root))
    app = build_web_app(context, static_dir=tmp_path / "no-dist")
    before = tree(tmp_path)
    async with open_client(app) as client:
        response = await client.get("/api/apply-health/jobs/1/form?batch_id=batch-one")
        data = response.json()
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert data["status"] == "available"
        latest = data["snapshots"][0]
        assert [item["value"] for item in latest["fields"]] == [
            False,
            0,
            "",
            ["A", "B"],
        ]
        assert latest["fields"][2]["state"] == "unanswered"
        assert latest["owner_thread_id"] == "owner-one"
        assert latest["attempt_id"] == "attempt-one"
        assert latest["source_url"] == "https://careers.example.test/contact"
        assert latest["resume"]["upload_status"] == "selected"
        artifact = await client.get(latest["resume"]["artifact_url"])
        assert artifact.content == PDF
        assert artifact.headers["content-type"] == "application/pdf"
        assert artifact.headers["cache-control"] == "no-store"
    assert "never-return-this" not in response.text
    assert "token=" not in response.text
    assert str(folder) not in response.text
    assert tree(tmp_path) == before


def test_pages_accumulate_and_attempts_and_owners_are_isolated(tmp_path):
    database, root, folder, scope = form_fixture(tmp_path)
    runs = [
        capture(scope, [field("Name", "Example")]),
        capture(
            scope, [field("Education", "Example school")], at=SECOND, page="education"
        ),
        capture(scope, [field("Name", "New attempt")], at=THIRD, attempt="attempt-two"),
    ]
    journal(folder, runs)
    snapshots = read_application_form(root, database, 1, "batch-one").snapshots
    assert [row.value for row in snapshots[0].fields] == ["New attempt"]
    assert [row.value for row in snapshots[1].fields] == ["Example", "Example school"]
    assert [row.captured_at for row in snapshots[1].fields] == [FIRST, SECOND]
    runs.append(
        capture(
            scope,
            [field("Name", "New owner")],
            at="2026-10-21T12:03:00Z",
            owner="owner-two",
        )
    )
    journal(folder, runs)
    assert [
        row.value
        for row in read_application_form(root, database, 1, "batch-one")
        .snapshots[0]
        .fields
    ] == ["New owner"]


def test_empty_capture_replaces_same_page_and_dynamic_ids_do_not_leave_ghosts(tmp_path):
    database, root, folder, scope = form_fixture(tmp_path)
    first = capture(scope, [field("Old field", "removed")])
    second = capture(scope, [], at=SECOND)
    second["pageKey"] = (
        "https://careers.example.test/contact|contact|different-controls"
    )
    journal(folder, [first, second])
    result = read_application_form(root, database, 1, "batch-one")
    assert result.status == "available"
    assert result.snapshots[0].fields == []
    assert result.snapshots[0].resume.filename == "selected.pdf"
    assert result.snapshots[1].fields[0].value == "removed"


@pytest.mark.parametrize(
    "coverage,truncated", [("unavailable", False), ("visible_controls", True)]
)
def test_failed_or_truncated_capture_is_partial(tmp_path, coverage, truncated):
    database, root, folder, scope = form_fixture(tmp_path)
    first = capture(scope, [field("Name", "Earlier")])
    second = capture(scope, [], at=SECOND)
    second["formSnapshot"].update(coverage=coverage, truncated=truncated)
    journal(folder, [first, second])
    result = read_application_form(root, database, 1, "batch-one")
    assert result.status == "partial"
    assert result.snapshots[0].coverage == "partial_history"
    if coverage == "unavailable":
        assert result.snapshots[0].fields[0].captured_at == FIRST


def test_legacy_records_remain_unbound_and_manual_history_is_not_forced_latest(
    tmp_path,
):
    database, root, folder, scope = form_fixture(tmp_path)
    legacy = {
        "at": FIRST,
        "pageUrl": "https://careers.example.test/contact",
        "fields": [
            {
                "fieldId": "Name",
                "question": "Name",
                "writtenAnswer": "Written",
                "verifiedAnswer": "Observed",
                "verification": "verified",
            }
        ],
    }
    manual = [
        {
            "fieldId": "Optional",
            "question": "Optional",
            "answer": "Attempt",
            "verifiedValue": "Not verified",
            "status": "failed",
            "recorded_at": SECOND,
        },
        {
            "fieldId": "Consent",
            "question": "Consent",
            "answer": False,
            "verificationValue": False,
            "status": "verified",
            "verifiedAt": SECOND,
        },
    ]
    journal(
        folder,
        [legacy, capture(scope, [field("Name", "Latest")], at=THIRD)],
        manualCorrections=manual,
    )
    result = read_application_form(root, database, 1, "batch-one")
    assert result.snapshots[0].provenance == "captured"
    corrected = next(row for row in result.snapshots if row.id == "manual-corrections")
    assert corrected.owner_thread_id is None
    assert corrected.resume is None
    assert [(row.value, row.state) for row in corrected.fields[-2:]] == [
        (None, "written_unverified"),
        (False, "observed"),
    ]
    historical = next(row for row in result.snapshots if row.id == "observation-0")
    assert historical.owner_thread_id is None
    assert historical.resume is None


def test_resume_only_record_is_partial_and_does_not_claim_upload(tmp_path):
    database, root, folder, _ = form_fixture(tmp_path)
    journal(folder, [])
    result = read_application_form(root, database, 1, "batch-one")
    assert result.status == "partial"
    assert result.snapshots[0].id == "resume-selection"
    assert result.snapshots[0].owner_thread_id is None
    assert result.snapshots[0].resume.upload_status == "selected"


def test_resume_is_available_without_answer_journal(tmp_path):
    database, root, _, _ = form_fixture(tmp_path)
    result = read_application_form(root, database, 1, "batch-one")
    assert result.status == "partial"
    assert result.snapshots[0].id == "resume-selection"
    assert read_form_resume(root, database, 1, "batch-one", "resume-selection") == PDF


def test_missing_journal_and_resume_remain_missing(tmp_path):
    database, root, folder, _ = form_fixture(tmp_path)
    (folder / "resume-source.json").unlink()
    result = read_application_form(root, database, 1, "batch-one")
    assert result.status == "missing"
    assert result.snapshots == []


def test_empty_and_masked_values_are_not_claimed_observed(tmp_path):
    database, root, folder, scope = form_fixture(tmp_path)
    journal(
        folder,
        [
            capture(
                scope,
                [
                    field("Empty string", ""),
                    field("No choices", []),
                    field("Masked", "<redacted>"),
                    field("Truncated", "partial", truncated=True),
                ],
            )
        ],
    )
    latest = read_application_form(root, database, 1, "batch-one").snapshots[0]
    assert [(row.value, row.state) for row in latest.fields] == [
        ("", "unanswered"),
        ([], "unanswered"),
        (None, "unavailable"),
        (None, "unavailable"),
    ]


def test_unknown_capture_identity_cannot_inherit_current_owner(tmp_path):
    database, root, folder, scope = form_fixture(tmp_path)
    run = capture(scope, [field("Name", "Unattributed")])
    run["formSnapshot"].pop("ownerThreadId")
    run["formSnapshot"].pop("attemptId")
    journal(folder, [run])
    latest = read_application_form(root, database, 1, "batch-one").snapshots[0]
    assert latest.owner_thread_id is None
    assert latest.attempt_id is None
    assert latest.resume is None


def test_capture_without_scope_requires_observed_owner_binding(tmp_path):
    database, root, folder, scope = form_fixture(tmp_path)
    run = capture(scope, [field("Name", "Other owner private value")], owner="foreign")
    run["formSnapshot"].pop("scope")
    journal(folder, [run])
    assert (
        "Other owner private value"
        not in read_application_form(root, database, 1, "batch-one").model_dump_json()
    )


def test_malformed_capture_never_falls_back_to_legacy_fields(tmp_path):
    database, root, folder, scope = form_fixture(tmp_path)
    run = capture(scope, [field("Name", "Value")])
    run["formSnapshot"] = "malformed"
    run["fields"] = [
        {
            "fieldId": "Name",
            "question": "Name",
            "verifiedAnswer": "Wrong fallback",
            "verification": "verified",
        }
    ]
    journal(folder, [run])
    result = read_application_form(root, database, 1, "batch-one")
    assert result.status == "unavailable"
    assert result.snapshots == []


def test_credentials_are_filtered_in_all_record_shapes(tmp_path):
    database, root, folder, scope = form_fixture(tmp_path)
    current = capture(
        scope,
        [
            field("Name", "Visible"),
            field("Password", "secret"),
            field("OTP", "secret"),
            field("ordinary", "secret", type="password"),
        ],
    )
    legacy = {
        "fields": [
            {
                "fieldId": "verification_code",
                "question": "Code",
                "verifiedAnswer": "secret",
                "verification": "verified",
            }
        ]
    }
    manual = [
        {"fieldId": "password", "question": "Account password", "answer": "secret"}
    ]
    journal(
        folder,
        [legacy, current],
        manualCorrections=manual,
        account={"password": "secret"},
    )
    result = read_application_form(root, database, 1, "batch-one")
    assert "secret" not in result.model_dump_json()
    assert [row.value for row in result.snapshots[0].fields] == ["Visible"]


@pytest.mark.parametrize(
    "question",
    [
        "Security code",
        "Authentication code",
        "secret_key",
        "access_token",
        "login_otp",
        "API key",
    ],
)
def test_legacy_and_manual_credential_aliases_are_never_returned(tmp_path, question):
    database, root, folder, _ = form_fixture(tmp_path)
    journal(
        folder,
        [
            {
                "at": FIRST,
                "fields": [
                    {
                        "fieldId": question,
                        "question": question,
                        "verifiedAnswer": "123456",
                        "verification": "verified",
                    }
                ],
            }
        ],
        manualCorrections=[
            {
                "fieldId": question,
                "question": question,
                "answer": "123456",
                "verifiedValue": "123456",
                "status": "verified",
            }
        ],
    )
    assert (
        "123456"
        not in read_application_form(root, database, 1, "batch-one").model_dump_json()
    )


@pytest.mark.parametrize("failure", ["unavailable", "truncated"])
def test_incomplete_prior_page_keeps_combined_snapshot_partial(tmp_path, failure):
    database, root, folder, scope = form_fixture(tmp_path)
    contact = capture(scope, [field("Name", "Earlier")])
    if failure == "truncated":
        contact["formSnapshot"]["truncated"] = True
    else:
        contact["formSnapshot"]["coverage"] = "unavailable"
    education = capture(
        scope, [field("School", "Example")], page="education", at=SECOND
    )
    journal(folder, [contact, education])
    result = read_application_form(root, database, 1, "batch-one")
    assert result.status == "partial"
    assert result.snapshots[0].coverage == "partial_history"
    repaired = capture(scope, [field("Name", "Current")], at=THIRD)
    journal(folder, [contact, education, repaired])
    assert read_application_form(root, database, 1, "batch-one").status == "available"


@pytest.mark.parametrize(
    "change",
    [
        "wrong_batch",
        "wrong_job",
        "wrong_scope",
        "outside_folder",
        "symlink_folder",
        "invalid_json",
        "too_large",
    ],
)
def test_invalid_or_unbound_records_do_not_expose_fields(tmp_path, change):
    database, root, folder, scope = form_fixture(tmp_path)
    run = capture(scope, [field("Name", "Private value")])
    journal(folder, [run])
    batch = "batch-one"
    if change == "wrong_batch":
        batch = "../batch-one"
    elif change == "wrong_job":
        scope.write_text(json.dumps({"ordered_candidates": [{"id": 2}]}))
    elif change == "wrong_scope":
        run["formSnapshot"]["scope"] = str(root / ".pipeline/batches/other.json")
        journal(folder, [run])
    elif change == "outside_folder":
        with sqlite3.connect(database) as db:
            db.execute(
                "UPDATE application_queue SET application_folder_relpath="
                "'../../outside' WHERE job_id=1"
            )
    elif change == "symlink_folder":
        folder.rename(root / "other")
        folder.symlink_to(root / "other", target_is_directory=True)
    elif change == "invalid_json":
        (folder / "application-answers.json").write_text("not json")
    else:
        (folder / "application-answers.json").write_bytes(b" " * (4 * 1024 * 1024 + 1))
    result = read_application_form(root, database, 1, batch)
    assert "Private value" not in result.model_dump_json()
    assert result.status in {"unavailable", "missing", "partial"}


@pytest.mark.parametrize(
    "change", ["traversal", "absolute", "symlink", "invalid_pdf", "wrong_selection"]
)
def test_resume_download_fails_closed(tmp_path, change):
    database, root, folder, scope = form_fixture(tmp_path)
    run = capture(scope, [field("Name", "Visible")])
    receipt_path = folder / "resume-source.json"
    receipt = json.loads(receipt_path.read_text())
    if change == "traversal":
        receipt["assets"]["pdf"]["path"] = "../../outside.pdf"
    elif change == "absolute":
        receipt["assets"]["pdf"]["path"] = str(folder / "selected.pdf")
    elif change == "symlink":
        (folder / "selected.pdf").unlink()
        (root / "outside.pdf").write_bytes(PDF)
        (folder / "selected.pdf").symlink_to(root / "outside.pdf")
    elif change == "invalid_pdf":
        (folder / "selected.pdf").write_bytes(b"not a PDF")
    else:
        run["selectedResume"]["resumeId"] = "other-selection"
    receipt_path.write_text(json.dumps(receipt))
    journal(folder, [run])
    result = read_application_form(root, database, 1, "batch-one")
    assert result.snapshots[0].resume.artifact_url is None
    with pytest.raises((ValueError, OSError)):
        read_form_resume(root, database, 1, "batch-one", "observation-0")


def test_upload_evidence_is_separate_from_selection(tmp_path):
    database, root, folder, scope = form_fixture(tmp_path)
    run = capture(scope, [field("Resume", ["selected.pdf"], type="file")])
    run["fields"] = [
        {
            "fieldId": "mapped:resume",
            "verifiedAnswer": "selected.pdf",
            "uploadStatus": "uploaded",
        }
    ]
    journal(folder, [run])
    result = read_application_form(root, database, 1, "batch-one")
    assert result.snapshots[0].resume.upload_status == "uploaded"
