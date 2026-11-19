import copy
from datetime import datetime, timedelta, timezone
from html import escape
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from pipeline.application_readiness import (
    ApplicationReadiness,
    ReadinessBridge,
    check,
    origin_url,
    preparation_script,
    probe_script,
    verified_probe,
    local_checks,
    coordination_check,
)
from pipeline.claude_codex_bridge import (
    InspectionBridge,
    read_turn_lifecycle,
)
from pipeline.jobfeed import inspection_url

ORIGIN = "http://127.0.0.1:7654"
NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)


class ReadinessFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "resume-application-pipeline"
        self.root.mkdir()
        (self.root / "pipeline").mkdir()
        (self.root / "pipeline/cli.py").write_text("")
        self.database = self.root / "jobfeed.sqlite"
        self.documents = {
            "relay": {
                "thread": {
                    "id": "relay",
                    "name": "Application readiness relay",
                    "originator": "Codex Desktop",
                    "cwd": str(self.root),
                    "turns": [{"status": "completed"}],
                }
            }
        }
        self.sender = Mock(return_value="queued-one")
        self.reader = Mock(side_effect=lambda name: copy.deepcopy(self.documents[name]))
        self.bridge = ReadinessBridge(
            self.root,
            reader=self.reader,
            sender=self.sender,
            terminal_reader=lambda *args: {
                "state": "completed",
                "source": "host_rollout",
            },
        )
        self.bridge.register_readiness(
            project_id="project",
            relay="relay",
            extension_instance_id="extension-one",
            profile_name="Personal",
            origin=ORIGIN,
        )
        self.now = NOW
        self.coordinator = ApplicationReadiness(
            self.root, self.database, bridge=self.bridge, clock=lambda: self.now
        )
        self.local = patch(
            "pipeline.application_readiness.local_checks",
            return_value=[check("profile", "ready", "Fixture profile validated.", NOW)],
        )
        self.local.start()
        self.addCleanup(self.local.stop)

    def start(self):
        return self.coordinator.start(str(uuid.uuid4()), ORIGIN)

    def proof_turn(self, request):
        config = self.bridge.config()
        proof = {
            "request_id": request["request_id"],
            "browser": {
                "family": "chrome",
                "type": "extension",
                "id": "dynamic-7",
                "profileName": "Personal",
                "metadata": {"extensionInstanceId": "extension-one"},
            },
            "tab_id": "own-tab",
            "marker": {
                "url": request["url"],
                "marker": request["url"].rsplit("/", 1)[1],
            },
            "file": {"input_type": "file", "name": "readiness-fixture.pdf"},
            "closed": True,
        }

        def call(code, value, ident):
            return {
                "type": "mcpToolCall",
                "id": ident,
                "server": "node_repl",
                "tool": "js",
                "status": "completed",
                "arguments": {"code": code},
                "error": None,
                "result": {"content": [{"type": "text", "text": json.dumps(value)}]},
            }

        items = [
            call(
                preparation_script(config, request["request_id"]),
                {
                    "readiness_prepared": request["request_id"],
                    "browser_documentation": "Official browser docs",
                    "file_upload_documentation": "Official upload docs",
                },
                "prep",
            ),
            call(
                probe_script(
                    config,
                    request["request_id"],
                    request["url"],
                    self.bridge.fixture_pdf(request["request_id"]),
                ),
                proof,
                "proof",
            ),
        ]
        return {"id": "turn-one", "status": "completed", "items": items}

    def finish(self, run, mutate=None, *, target_root=None):
        self.bridge.relay_next("relay")
        with self.bridge.db() as db:
            request_id = db.execute(
                "SELECT request_id FROM bridge_requests"
            ).fetchone()[0]
        request = self.bridge.request(request_id)
        turn = self.proof_turn(request)
        turn["items"].insert(
            0,
            {
                "type": "functionCallOutput",
                "namespace": "codex_app",
                "name": "create_thread",
                "output": "<codex_delegation><source_thread_id>relay</source_thread_id><input>"
                + escape(request["prompt"])
                + "</input></codex_delegation>",
            },
        )
        turn["items"].append(
            {
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "Diagnostic complete.",
            }
        )
        if mutate is not None:
            mutate(turn)
        self.documents["target"] = {
            "thread": {
                "id": "target",
                "originator": "Codex Desktop",
                "cwd": str(target_root or self.root),
                "createdAt": datetime.now(timezone.utc).timestamp(),
                "turns": [turn],
            }
        }
        self.bridge.acknowledge(request_id, "relay", "target")
        return self.coordinator.get(run["run_id"])


class ReadinessTests(ReadinessFixture):
    def escaped_preparation_turn(self, turn):
        envelope, preparation, _, final = turn["items"]
        envelope["output"] = envelope["output"].replace("</input>", "broken carrier</input>")
        preparation["arguments"]["code"] = json.dumps(preparation["arguments"]["code"])[1:-1]
        preparation["status"] = "failed"
        preparation["result"] = {
            "content": [{"type": "text", "text": "Illegal Unicode escape sequence"}],
            "_meta": {"codex/telemetry": {"error_code": "syntax_error"}},
        }
        turn["items"] = [envelope, preparation, final]

    def test_escaped_preparation_is_blocked_with_verified_no_execution(self):
        completed = self.finish(self.start(), self.escaped_preparation_turn)
        self.assertEqual(completed["phase"], "blocked")
        self.assertFalse(completed["ready"])
        with self.bridge.db() as db:
            request_id = db.execute("SELECT request_id FROM bridge_requests").fetchone()[0]
        outcome = self.bridge.get(request_id)
        self.assertEqual(outcome["result"], {"page_state": "blocked", "extension": False, "upload": False, "cleanup": True})
        self.assertFalse(outcome["host_evidence"]["creation_verified"])
        self.assertTrue(outcome["host_evidence"]["preparation_syntax_error"])
        self.assertEqual(outcome["verification_error"], "unique initial official create_thread provenance is unverified")
        self.assertEqual({c["id"]: c["status"] for c in completed["checks"]}["harness"], "blocked")

    def test_syntax_failure_recovery_rejects_unverified_or_executable_actions(self):
        mutations = {
            "missing structured error": lambda turn: turn["items"][1]["result"].pop("_meta"),
            "different script": lambda turn: turn["items"][1]["arguments"].update(code="throw new Error('syntax_error')"),
            "completed tool": lambda turn: turn["items"][1].update(status="completed"),
            "wrong tool": lambda turn: turn["items"][1].update(server="cua_repl"),
            "extra action": lambda turn: turn["items"].append({"type": "commandExecution", "command": "echo bad"}),
            "wrong source": lambda turn: turn["items"][0].update(output=turn["items"][0]["output"].replace("<source_thread_id>relay", "<source_thread_id>other")),
            "changed request": lambda turn: turn["items"][0].update(output=turn["items"][0]["output"].replace("harmless readiness", "changed readiness")),
            "no final": lambda turn: turn["items"].pop(),
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                case = ReadinessFixture()
                case.setUp()
                try:
                    def mutate(turn):
                        self.escaped_preparation_turn(turn)
                        mutation(turn)
                    completed = case.finish(case.start(), mutate)
                    self.assertEqual(completed["phase"], "dispatch_unknown")
                    self.assertFalse(completed["ready"])
                finally:
                    case.doCleanups()

    def test_syntax_failure_requires_original_completed_host_turn(self):
        self.bridge.terminal_reader = lambda *args: {"state": "unverified"}
        completed = self.finish(self.start(), self.escaped_preparation_turn)
        self.assertEqual(completed["phase"], "dispatch_unknown")

    def test_syntax_failure_cannot_ignore_later_turns(self):
        reader = self.bridge.reader
        def with_later_turn(name):
            data = reader(name)
            if name == "target":
                data["thread"]["turns"].append({"id": "later", "status": "completed", "items": []})
            return data
        self.bridge.reader = with_later_turn
        completed = self.finish(self.start(), self.escaped_preparation_turn)
        self.assertEqual(completed["phase"], "dispatch_unknown")

    def register_combined(self):
        project_root = self.root.parent
        self.documents["relay"]["thread"]["cwd"] = str(project_root)
        self.bridge.register_readiness(
            project_id="combined-project",
            project_root=project_root,
            relay="relay",
            extension_instance_id="extension-one",
            profile_name="Personal",
            origin=ORIGIN,
            replace=True,
        )
        return project_root

    def test_combined_project_keeps_artifacts_under_pipeline_runtime(self):
        project_root = self.register_combined()
        lifecycle = Mock(return_value={"state": "completed", "source": "host_rollout"})
        self.bridge.terminal_reader = lifecycle
        self.assertEqual(self.bridge.config()["project_root"], str(project_root))
        self.assertEqual(self.bridge.path.parent, self.root / ".pipeline")
        run = self.start()
        self.assertEqual(run["phase"], "running")
        completed = self.finish(run, target_root=project_root)
        self.assertTrue(completed["ready"])
        self.assertEqual(lifecycle.call_args.args[3], str(project_root))
        self.assertEqual(self.sender.call_args.args[2], self.root)
        self.assertFalse((project_root / ".pipeline").exists())

    def test_combined_project_rejects_runtime_child_as_target_cwd(self):
        self.register_combined()
        run = self.start()
        completed = self.finish(run)
        self.assertFalse(completed["ready"])
        with self.bridge.db() as db:
            row = db.execute("SELECT verification_error FROM bridge_requests").fetchone()
        self.assertEqual(
            row[0], "thread must belong to the configured Codex Desktop workspace"
        )

    def test_combined_project_requires_relay_to_keep_exact_registered_cwd(self):
        self.register_combined()
        self.documents["relay"]["thread"]["cwd"] = str(self.root)
        self.assertEqual(self.start()["phase"], "blocked")
        self.sender.assert_not_called()

    def test_combined_project_rejects_host_rollout_with_runtime_child_cwd(self):
        project_root = self.register_combined()
        rollout = self.root / "wrong-project-rollout.jsonl"
        rollout.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"id": "target", "cwd": str(self.root)},
                }
            )
            + "\n"
        )
        self.bridge.terminal_reader = lambda _, target, turn, expected_root: (
            read_turn_lifecycle(str(rollout), target, turn, expected_root)
        )
        completed = self.finish(self.start(), target_root=project_root)
        self.assertFalse(completed["ready"])
        with self.bridge.db() as db:
            row = db.execute("SELECT verification_error FROM bridge_requests").fetchone()
        self.assertEqual(row[0], "turn_lifecycle_unverified")

    def test_project_root_change_requires_explicit_registration_replacement(self):
        self.documents["relay"]["thread"]["cwd"] = str(self.root.parent)
        with self.assertRaisesRegex(ValueError, "--replace"):
            self.bridge.register_readiness(
                project_id="project",
                project_root=self.root.parent,
                relay="relay",
                extension_instance_id="extension-one",
                profile_name="Personal",
                origin=ORIGIN,
            )
        self.assertEqual(self.bridge.config()["project_root"], str(self.root))

    def test_unrelated_project_root_cannot_be_registered(self):
        unrelated = self.root.parent / "other-project"
        unrelated.mkdir()
        self.documents["relay"]["thread"]["cwd"] = str(unrelated)
        with self.assertRaisesRegex(ValueError, "must contain"):
            self.bridge.register_readiness(
                project_id="other-project",
                project_root=unrelated,
                relay="relay",
                extension_instance_id="extension-one",
                profile_name="Personal",
                origin=ORIGIN,
                replace=True,
            )
        self.assertEqual(self.bridge.config()["project_root"], str(self.root))

    def test_full_host_proof_promotes_required_checks_and_expires(self):
        run = self.start()
        self.assertEqual(run["phase"], "running")
        completed = self.finish(run)
        self.assertTrue(completed["ready"])
        self.assertEqual(
            {c["id"]: c["status"] for c in completed["checks"]},
            {
                "profile": "ready",
                "registration": "ready",
                "artifacts": "ready",
                "harness": "ready",
                "chrome": "ready",
                "upload": "ready",
                "cleanup": "ready",
            },
        )
        self.now += timedelta(seconds=301)
        expired = self.coordinator.get()
        self.assertEqual((expired["ready"], expired["stale"]), (False, True))
        self.assertEqual(self.sender.call_count, 1)

    def test_duplicate_restart_and_unknown_wake_never_send_again(self):
        self.sender.side_effect = TimeoutError("fixture lost receipt")
        run = self.start()
        self.assertEqual(run["phase"], "dispatch_unknown")
        restored = ApplicationReadiness(
            self.root, self.database, bridge=self.bridge, clock=lambda: self.now
        )
        duplicate = restored.start(str(uuid.uuid4()), ORIGIN)
        self.assertEqual(duplicate["run_id"], run["run_id"])
        self.assertFalse(duplicate["ready"])
        self.assertEqual(self.sender.call_count, 1)

    def test_missing_mapping_recovers_original_receipt_without_dispatch(self):
        run = self.start()
        with sqlite3.connect(self.coordinator.path) as db:
            db.execute("UPDATE readiness_runs SET bridge_request=NULL")
        restored = self.coordinator.get()
        self.assertEqual(restored["run_id"], run["run_id"])
        self.assertEqual(restored["phase"], "running")
        self.assertEqual(self.sender.call_count, 1)
        self.assertTrue(self.bridge.path.is_file())

    def test_late_failed_observation_cannot_overwrite_completed_run(self):
        run = self.start()
        completed = self.finish(run)
        run["phase"] = "cleanup_unknown"
        self.coordinator._save(run)
        observed = self.coordinator.get()
        self.assertEqual(observed, completed)
        self.assertTrue(observed["ready"])

    def test_profile_or_browser_binding_changes_invalidate_green(self):
        run = self.start()
        self.finish(run)
        (self.root / "application-profile.md").write_text("Synthetic changed profile")
        observed = self.coordinator.get()
        self.assertEqual((observed["ready"], observed["stale"]), (False, True))

    def test_future_time_never_passes(self):
        run = self.start()
        self.finish(run)
        self.now -= timedelta(seconds=31)
        self.assertEqual(
            (self.coordinator.get()["ready"], self.coordinator.get()["stale"]),
            (False, True),
        )

    def test_forged_or_wrong_profile_or_marker_or_cleanup_evidence_fails(self):
        run = self.start()
        with self.bridge.db() as db:
            request_id = db.execute(
                "SELECT request_id FROM bridge_requests"
            ).fetchone()[0]
        request = self.bridge.request(request_id)
        baseline = self.proof_turn(request)
        changes = [
            lambda t: t["items"][1]["arguments"].update(code='console.log("fake")'),
            lambda t: t["items"][1].pop("arguments"),
            lambda t: t["items"][0]["arguments"].update(
                code='console.log("fake docs")'
            ),
        ]
        for change in changes:
            turn = copy.deepcopy(baseline)
            change(turn)
            with self.subTest(change=change), self.assertRaises(ValueError):
                verified_probe(
                    turn,
                    request,
                    self.bridge.config(),
                    self.bridge.fixture_pdf(request_id),
                )
        for field, wrong in [
            ("closed", False),
            ("marker", {"url": ORIGIN, "marker": request_id}),
            ("browser", {"family": "chrome", "type": "iab", "id": "iab"}),
        ]:
            turn = copy.deepcopy(baseline)
            block = turn["items"][1]["result"]["content"][0]
            data = json.loads(block["text"])
            data[field] = wrong
            block["text"] = json.dumps(data)
            with self.subTest(field=field), self.assertRaises(ValueError):
                verified_probe(
                    turn,
                    request,
                    self.bridge.config(),
                    self.bridge.fixture_pdf(request_id),
                )
        final_only = {
            "items": [
                {
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": '{"ready":true}',
                }
            ]
        }
        with self.assertRaises(ValueError):
            verified_probe(
                final_only,
                request,
                self.bridge.config(),
                self.bridge.fixture_pdf(request_id),
            )
        self.assertFalse(run["ready"])

    def test_upload_without_selection_is_not_inferred_from_tab_proof(self):
        run = self.start()
        with self.bridge.db() as db:
            request_id = db.execute(
                "SELECT request_id FROM bridge_requests"
            ).fetchone()[0]
        request = self.bridge.request(request_id)
        turn = self.proof_turn(request)
        block = turn["items"][1]["result"]["content"][0]
        data = json.loads(block["text"])
        data["file"] = None
        block["text"] = json.dumps(data)
        result, _ = verified_probe(
            turn, request, self.bridge.config(), self.bridge.fixture_pdf(request_id)
        )
        self.assertEqual(
            result,
            {
                "page_state": "observed",
                "extension": True,
                "cleanup": True,
                "upload": False,
            },
        )
        self.assertFalse(run["ready"])

    def test_no_registration_keeps_local_results_and_safe_setup_instruction(self):
        self.bridge.path.unlink()
        run = self.start()
        self.assertEqual(run["phase"], "blocked")
        checks = {c["id"]: c for c in run["checks"]}
        self.assertEqual(checks["profile"]["status"], "ready")
        self.assertIn("--extension-instance-id", checks["registration"]["next_step"])
        self.assertEqual(self.sender.call_count, 0)

    def test_fixture_is_static_and_never_serves_profile_or_application_inputs(self):
        run = self.start()
        fixture = self.coordinator.fixture(run["run_id"])
        self.assertIn(
            '<input id="readiness-file" type="file" accept="application/pdf">', fixture
        )
        self.assertNotIn("<script", fixture)
        self.assertNotIn("<form", fixture)
        self.assertIn(run["run_id"], fixture)
        with self.assertRaises(ValueError):
            self.coordinator.fixture(str(uuid.uuid4()))

    def test_inspection_stays_https_only_and_policies_are_separate(self):
        with self.assertRaises(ValueError):
            inspection_url(ORIGIN + "/fixture")
        inspector = InspectionBridge(self.root, "application-readiness")
        inspector.path = self.bridge.path
        with self.assertRaisesRegex(ValueError, "policy mismatch"):
            inspector.config()
        with self.assertRaises(ValueError):
            origin_url("http://127.0.0.1.evil.example:7654")
        self.assertEqual(origin_url(ORIGIN), ORIGIN)

    def test_wrong_root_active_relay_and_application_controller_registration_fail(self):
        self.documents["relay"]["thread"]["turns"] = [{"status": "inProgress"}]
        with self.assertRaises(ValueError):
            self.bridge.register_readiness(
                project_id="project",
                relay="relay",
                extension_instance_id="other",
                profile_name="Personal",
                origin=ORIGIN,
            )
        self.documents["relay"]["thread"]["turns"] = [{"status": "completed"}]
        self.documents["relay"]["thread"]["cwd"] = str(self.root.parent)
        with self.assertRaises(ValueError):
            self.bridge.register_readiness(
                project_id="project",
                relay="relay",
                extension_instance_id="other",
                profile_name="Personal",
                origin=ORIGIN,
            )

    def test_missing_runtime_is_not_created_by_diagnostic(self):
        missing = self.root / "does-not-exist"
        coordinator = ApplicationReadiness(missing, self.database)
        with self.assertRaises(ValueError):
            coordinator.start(str(uuid.uuid4()), ORIGIN)
        self.assertFalse(missing.exists())
        self.assertTrue((self.root / "pipeline/cli.py").is_file())


class LocalReadinessTests(unittest.TestCase):
    def test_skeletal_database_cannot_pass_and_full_schema_is_read_without_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "jobs.sqlite"
            with sqlite3.connect(database) as db:
                db.executescript(
                    "CREATE TABLE jobs(id); CREATE TABLE applied(job_id); CREATE TABLE application_queue(job_id,state,codex_application_session_id);"
                )
            before = database.read_bytes()
            rows = local_checks(root, database, Path("/missing/python"), NOW)
            found = {c["id"]: c for c in rows}
            self.assertEqual(found["jobfeed_db"]["status"], "blocked")
            self.assertEqual(found["profile"]["status"], "blocked")
            self.assertEqual(found["resumes"]["status"], "blocked")
            self.assertEqual(database.read_bytes(), before)
            with sqlite3.connect(database) as db:
                db.executescript(
                    "ALTER TABLE jobs ADD real_job_id; ALTER TABLE jobs ADD company; ALTER TABLE jobs ADD title; ALTER TABLE jobs ADD jd_text; ALTER TABLE jobs ADD url; ALTER TABLE application_queue ADD retry_count; ALTER TABLE application_queue ADD next_retry_at; ALTER TABLE application_queue ADD application_folder_relpath; ALTER TABLE application_queue ADD submit_authorization;"
                )
            before = database.read_bytes()
            found = {
                c["id"]: c
                for c in local_checks(root, database, Path("/missing/python"), NOW)
            }
            self.assertEqual(found["jobfeed_db"]["status"], "ready")
            self.assertEqual(found["execution_python"]["status"], "blocked")
            self.assertEqual(found["execution_cli"]["status"], "blocked")
            self.assertEqual(database.read_bytes(), before)
            self.assertEqual(found["site"]["status"], "not_tested")
            self.assertFalse(found["site"]["required"])

    def test_unresolved_coordination_is_visible_and_never_released(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".pipeline").mkdir()
            database = root / ".pipeline/coordination.sqlite3"
            with sqlite3.connect(database) as db:
                db.executescript(
                    "CREATE TABLE coordination_claims(status); CREATE TABLE coordination_tabs(state); INSERT INTO coordination_claims VALUES('needs_reconciliation');"
                )
            before = database.read_bytes()
            row = coordination_check(root, NOW)
            self.assertEqual((row["status"], row["required"]), ("blocked", True))
            self.assertEqual(database.read_bytes(), before)


class ReadinessRebindingTests(ReadinessFixture):
    def test_post_registration_relay_application_binding_prevents_send(self):
        path = self.root / ".pipeline/blockers.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE dq_scopes(controller)")
            db.execute("INSERT INTO dq_scopes VALUES('relay')")
        run = self.start()
        self.assertEqual(run["phase"], "blocked")
        self.assertEqual(self.sender.call_count, 0)
        self.assertEqual(
            {c["id"]: c["status"] for c in run["checks"]}["registration"], "blocked"
        )

    def test_post_registration_active_or_renamed_relay_prevents_send(self):
        self.documents["relay"]["thread"]["turns"] = [{"status": "inProgress"}]
        run = self.start()
        self.assertEqual(run["phase"], "blocked")
        self.assertEqual(self.sender.call_count, 0)
        self.documents["relay"]["thread"]["turns"] = [{"status": "completed"}]
        self.documents["relay"]["thread"]["name"] = "Application controller"
        run = self.start()
        self.assertEqual(run["phase"], "blocked")
        self.assertEqual(self.sender.call_count, 0)

    def test_rebind_requires_explicit_request_and_verified_prior_cleanup(self):
        options = dict(
            project_id="project",
            relay="relay",
            extension_instance_id="extension-two",
            profile_name="Other",
            origin=ORIGIN,
        )
        with self.assertRaises(ValueError):
            self.bridge.register_readiness(**options)
        run = self.start()
        with self.assertRaises(ValueError):
            self.bridge.register_readiness(**options, replace=True)
        completed = self.finish(run)
        self.assertTrue(completed["ready"])
        self.bridge.register_readiness(**options, replace=True)
        observed = self.coordinator.get()
        self.assertEqual((observed["ready"], observed["stale"]), (False, True))
        self.assertEqual(self.bridge.config()["extension_instance_id"], "extension-two")

    def test_expiry_uses_earliest_required_evidence_not_delayed_completion(self):
        run = self.start()
        self.now += timedelta(seconds=301)
        completed = self.finish(run)
        self.assertEqual((completed["ready"], completed["stale"]), (False, True))
        self.assertEqual(completed["expires_at"], "2026-10-21T12:05:00+00:00")


class ReadinessFailedPreparationTests(ReadinessFixture):
    def test_exact_preparation_only_can_retry_without_an_unowned_tab(self):
        run = self.start()

        def preparation_only(turn):
            turn["items"] = [
                item for item in turn["items"] if item.get("id") != "proof"
            ]
            prep = next(item for item in turn["items"] if item.get("id") == "prep")
            prep["result"] = {
                "isError": True,
                "content": [{"type": "text", "text": "browser binding unavailable"}],
            }
            prep["error"] = "browser binding unavailable"

        observed = self.finish(run, mutate=preparation_only)
        self.assertEqual(observed["phase"], "blocked")
        statuses = {c["id"]: c["status"] for c in observed["checks"]}
        self.assertEqual(
            (statuses["chrome"], statuses["upload"], statuses["cleanup"]),
            ("blocked", "not_tested", "ready"),
        )
        self.assertFalse(observed["ready"])
        retried = self.start()
        self.assertNotEqual(retried["run_id"], run["run_id"])
        self.assertEqual(self.sender.call_count, 2)

    def test_extra_tool_action_prevents_claiming_no_tab_created(self):
        run = self.start()

        def extra_action(turn):
            turn["items"] = [
                item for item in turn["items"] if item.get("id") != "proof"
            ]
            turn["items"].append(
                {
                    "type": "functionCall",
                    "name": "functions.exec",
                    "arguments": "unknown browser operation",
                }
            )

        observed = self.finish(run, mutate=extra_action)
        self.assertEqual(observed["phase"], "cleanup_unknown")
        self.assertFalse(observed["ready"])
        retried = self.start()
        self.assertEqual(retried["run_id"], run["run_id"])
        self.assertEqual(self.sender.call_count, 1)


class ReadinessActionPolicyTests(ReadinessFixture):
    def evidence(self):
        self.start()
        with self.bridge.db() as db:
            request_id = db.execute(
                "SELECT request_id FROM bridge_requests"
            ).fetchone()[0]
        request = self.bridge.request(request_id)
        return request, self.proof_turn(request)

    def test_exact_carriers_and_inert_discovery_preserve_direct_node_proof(self):
        from pipeline.application_readiness import DISCOVERY_SCRIPT, carrier_script

        request, turn = self.evidence()
        prep, proof = turn["items"]

        def carrier(code):
            return {
                "type": "functionCall",
                "namespace": "functions",
                "name": "exec",
                "arguments": code,
            }

        turn["items"] = [
            carrier(DISCOVERY_SCRIPT),
            carrier(carrier_script(prep["arguments"]["code"], preparation=True)),
            prep,
            carrier(carrier_script(proof["arguments"]["code"])),
            proof,
        ]
        result, evidence = verified_probe(
            turn,
            request,
            self.bridge.config(),
            self.bridge.fixture_pdf(request["request_id"]),
        )
        self.assertEqual(
            result,
            {
                "page_state": "observed",
                "extension": True,
                "cleanup": True,
                "upload": True,
            },
        )
        self.assertTrue(evidence["owned_tab_closed"])

    def test_extra_browser_or_arbitrary_exec_or_command_rejects_green(self):
        request, baseline = self.evidence()
        extra = [
            {
                "type": "mcpToolCall",
                "server": "cua_repl",
                "tool": "js",
                "status": "completed",
                "arguments": {"code": "await cua.getState();"},
            },
            {
                "type": "functionCall",
                "namespace": "functions",
                "name": "exec",
                "arguments": 'text(await tools.exec_command({cmd:"open employer"}));',
            },
            {"type": "commandExecution", "command": "open employer"},
            {"type": "dynamicToolCall", "tool": "unknown", "arguments": {}},
        ]
        for action in extra:
            turn = copy.deepcopy(baseline)
            turn["items"].insert(1, action)
            with (
                self.subTest(action=action),
                self.assertRaisesRegex(ValueError, "unsupported_diagnostic_action"),
            ):
                verified_probe(
                    turn,
                    request,
                    self.bridge.config(),
                    self.bridge.fixture_pdf(request["request_id"]),
                )

    def test_altered_carrier_cannot_change_environment_before_exact_probe(self):
        from pipeline.application_readiness import carrier_script

        request, turn = self.evidence()
        carrier = (
            carrier_script(turn["items"][0]["arguments"]["code"], preparation=True)
            + "\nglobalThis.readinessBrowser = fakeBrowser;"
        )
        turn["items"].insert(
            0,
            {
                "type": "functionCall",
                "namespace": "functions",
                "name": "exec",
                "arguments": carrier,
            },
        )
        with self.assertRaisesRegex(ValueError, "unsupported_diagnostic_action"):
            verified_probe(
                turn,
                request,
                self.bridge.config(),
                self.bridge.fixture_pdf(request["request_id"]),
            )
