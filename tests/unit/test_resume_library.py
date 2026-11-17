"""Uploaded filename identity, active choices, and immutable application sources."""

import json
from concurrent.futures import ThreadPoolExecutor
from shutil import copyfile

import pytest

from jobfeed.my_profile import MyProfileService
from jobfeed.onboarding_resume import ResumeDraftStore, ResumeOnboardingService
from jobfeed.onboarding_resume_files import ResumeFileStore
from jobfeed.onboarding_resume_types import ResumeDraftState
from tests.unit.test_onboarding_resume import _pdf_bytes


def _services(tmp_path):
    root = tmp_path / "jobfeed"
    pipeline = tmp_path / "pipeline"
    pipeline.mkdir()
    files = ResumeFileStore(root / "data/resumes")
    drafts = ResumeDraftStore(root / "data/onboarding-resume.json")
    onboarding = ResumeOnboardingService(
        files=files, drafts=drafts, analyzer=None, provider_state=lambda: None
    )
    profile = MyProfileService(
        root=root, pipeline_root=pipeline, resume_state=onboarding.state
    )
    return files, drafts, onboarding, profile


def test_same_filename_replaces_one_entry_and_keeps_old_bytes(tmp_path):
    files, _, _, _ = _services(tmp_path)
    old = files.save("Resume.pdf", _pdf_bytes("Earlier version"))
    frozen = tmp_path / "frozen.pdf"
    copyfile(old.path, frozen)
    identifier = files.library()[0]["id"]
    new = files.save("resume.PDF", _pdf_bytes("Updated version"))
    entries = files.library()
    assert len(entries) == 1
    assert entries[0]["id"] == identifier
    assert entries[0]["original_name"] == "resume.PDF"
    assert entries[0]["stored_name"] == new.stored_name
    assert old.path.read_bytes() == frozen.read_bytes()
    assert new.path != old.path
    assert b"Updated version" in new.path.read_bytes()
    assert len(files.library()) == 1


def test_different_names_retained_and_inactive_replacement_stays_inactive(tmp_path):
    files, _, _, _ = _services(tmp_path)
    files.save("Backend.pdf", _pdf_bytes("Backend"))
    files.save("Frontend.pdf", _pdf_bytes("Frontend"))
    row = next(row for row in files.library() if row["original_name"] == "Backend.pdf")
    files.set_active(row["id"], False)
    files.save("Backend.pdf", _pdf_bytes("Backend revised"))
    entries = files.library()
    assert {row["original_name"] for row in entries} == {"Backend.pdf", "Frontend.pdf"}
    assert next(item for item in entries if item["id"] == row["id"])["active"] is False


def test_legacy_duplicates_merge_by_recorded_original_filename(tmp_path):
    _, drafts, _, profile = _services(tmp_path)
    upload = profile.root / "data/resumes/legacy-prefix-Resume.pdf"
    upload.parent.mkdir(parents=True)
    upload.write_bytes(_pdf_bytes("Old upload"))
    drafts._write(
        ResumeDraftState(
            stored_name=upload.name,
            original_name="Resume.pdf",
            extracted_text="Old upload",
        )
    )
    external = tmp_path / "Desktop/Resume.pdf"
    external.parent.mkdir()
    external.write_bytes(upload.read_bytes())
    old_copy = profile.pipeline_root / "old-copy.pdf"
    old_copy.write_bytes(upload.read_bytes())
    profile.catalog_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "resumes": [
                    {
                        "id": "external",
                        "label": "User-selected Desktop resume",
                        "pdf": str(external),
                        "available": True,
                    },
                    {
                        "id": "profile-old",
                        "label": "Resume.pdf",
                        "pdf": str(old_copy),
                        "profile_upload": upload.name,
                        "available": True,
                    },
                ],
            }
        )
    )
    result = profile.state()
    assert len(result.resumes) == 1
    assert result.resumes[0].filename == "Resume.pdf"
    assert len(json.loads(profile.catalog_path.read_text())["resumes"]) == 1
    assert external.exists() and old_copy.exists()


def test_upgrade_first_action_replacement_recovers_previous_draft_name(tmp_path):
    _, drafts, onboarding, profile = _services(tmp_path)
    old = profile.root / "data/resumes/legacy-prefix-Resume.pdf"
    old.parent.mkdir(parents=True)
    old.write_bytes(_pdf_bytes("Old original"))
    drafts._write(
        ResumeDraftState(
            stored_name=old.name,
            original_name="Resume.pdf",
            extracted_text="Old original",
        )
    )
    onboarding.upload("Resume.pdf", _pdf_bytes("Replacement"))
    result = profile.state()
    assert [row.filename for row in result.resumes] == ["Resume.pdf"]
    assert old.exists()


def test_inactive_legacy_catalog_choice_is_preserved_on_migration_and_replacement(
    tmp_path,
):
    _, drafts, onboarding, profile = _services(tmp_path)
    old = profile.root / "data/resumes/legacy-prefix-Resume.pdf"
    old.parent.mkdir(parents=True)
    old.write_bytes(_pdf_bytes("Inactive original"))
    drafts._write(
        ResumeDraftState(
            stored_name=old.name,
            original_name="Resume.pdf",
            extracted_text="Inactive original",
        )
    )
    profile.catalog_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "resumes": [
                    {
                        "id": "legacy",
                        "label": "Resume.pdf",
                        "pdf": str(old),
                        "profile_upload": old.name,
                        "available": False,
                    }
                ],
            }
        )
    )
    # Upload is the first action. Metadata is read without first mutating the library.
    onboarding.upload(
        "Resume.pdf",
        _pdf_bytes("Replacement"),
        library_metadata=profile.upload_metadata(),
    )
    result = profile.state()
    assert len(result.resumes) == 1
    assert result.resumes[0].active is False
    assert result.application_resumes[0].available is False
    assert result.resumes[0].status == "Inactive for future applications"


def test_missing_inactive_pdf_is_visible_without_breaking_other_rows(tmp_path):
    _, _, onboarding, profile = _services(tmp_path)
    uploaded = onboarding.upload("One.pdf", _pdf_bytes("One"))
    onboarding.upload("Two.pdf", _pdf_bytes("Two"))
    state = profile.state()
    one = next(row for row in state.resumes if row.filename == "One.pdf")
    profile.set_active(one.id, False)
    (profile.root / "data/resumes" / uploaded.stored_name).unlink()
    state = profile.state()
    row = next(row for row in state.resumes if row.id == one.id)
    assert row.active is False
    assert row.application_supported is False
    assert "unavailable" in row.status
    assert sorted(row.filename for row in state.resumes) == ["One.pdf", "Two.pdf"]


def test_nonpdf_scoring_upload_is_listed_without_fake_application_pdf(tmp_path):
    _, _, onboarding, profile = _services(tmp_path)
    onboarding.upload("Experience.md", b"Actual experience")
    state = profile.state()
    assert state.application_resumes == []
    assert state.resumes[0].format == "md"
    assert state.resumes[0].application_supported is False
    assert "Scoring only" in state.resumes[0].status
    assert not profile.catalog_path.exists()


def test_invalid_replacement_does_not_mutate_library_catalog_or_files(tmp_path):
    files, _, onboarding, profile = _services(tmp_path)
    onboarding.upload("Resume.pdf", _pdf_bytes("Valid content"))
    profile.state()
    before = {
        path: path.read_bytes() for path in profile.root.rglob("*") if path.is_file()
    }
    catalog = profile.catalog_path.read_bytes()
    with pytest.raises(ValueError):
        onboarding.upload(
            "Resume.pdf", b"not a PDF", library_metadata=profile.upload_metadata()
        )
    assert profile.catalog_path.read_bytes() == catalog
    assert before == {
        path: path.read_bytes() for path in profile.root.rglob("*") if path.is_file()
    }
    assert len(files.library()) == 1


def test_parallel_saves_keep_different_names_and_one_same_name_entry(tmp_path):
    files, _, _, _ = _services(tmp_path)
    names = ["One.pdf", "Two.pdf", "Same.pdf", "same.PDF"]
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(lambda name: files.save(name, _pdf_bytes(name)), names))
    rows = files.library()
    assert sorted(row["original_name"].casefold() for row in rows) == [
        "one.pdf",
        "same.pdf",
        "two.pdf",
    ]
    assert len({row["id"] for row in rows}) == len(rows)
    assert len(
        list((tmp_path / "jobfeed/data/resumes").glob("*.pdf"))
        + list((tmp_path / "jobfeed/data/resumes").glob("*.PDF"))
    ) == len(names)


def test_same_name_replacement_does_not_carry_old_facts_or_text(tmp_path):
    _, _, onboarding, profile = _services(tmp_path)
    onboarding.upload("Resume.pdf", _pdf_bytes("Old content"))
    profile.state()
    catalog = json.loads(profile.catalog_path.read_text())
    catalog["resumes"][0].update(
        autofill="old-facts.json",
        text="old-text.md",
        description="Old experience",
        custom_note="Keep note",
    )
    profile.catalog_path.write_text(json.dumps(catalog))
    onboarding.upload(
        "Resume.pdf",
        _pdf_bytes("New content"),
        library_metadata=profile.upload_metadata(),
    )
    profile.state()
    row = json.loads(profile.catalog_path.read_text())["resumes"][0]
    assert "autofill" not in row and "text" not in row and "description" not in row
    assert row["custom_note"] == "Keep note"


def test_parallel_workflow_upload_keeps_latest_draft_and_library_consistent(tmp_path):
    files, drafts, onboarding, _ = _services(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(
            workers.map(
                lambda text: onboarding.upload("Same.pdf", _pdf_bytes(text)),
                ["First", "Second", "Third", "Fourth"],
            )
        )
    draft = drafts.load()
    rows = files.library()
    assert len(rows) == 1
    assert draft.stored_name == rows[0]["stored_name"]
    assert (
        draft.extracted_text.encode()
        in (tmp_path / "jobfeed/data/resumes" / draft.stored_name).read_bytes()
    )
