"""Local prerequisites and an isolated, host-verified Desktop browser diagnostic."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
from html import unescape
import json
import os
from pathlib import Path
import shlex
import re
import shutil
import sqlite3
import subprocess
import sys
from urllib.parse import urlsplit
import uuid

from pipeline.claude_codex_bridge import (
    InspectionBridge,
    creation_turn,
    desktop_thread,
    identifier,
)
from pipeline.config import project_directory, runtime_command
from pipeline.claude_codex_bridge import CODEX_BIN

TTL_SECONDS = 300
PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
BROWSER_CLIENT = (
    Path.home()
    / ".codex/plugins/cache/openai-bundled/chrome/latest/scripts/browser-client.mjs"
)
ACTIVE = {"running", "dispatch_unknown", "cleanup_unknown"}


def read_desktop_thread(thread_id):
    from pipeline.codex_app_server import CodexAppServer

    with CodexAppServer(
        (CODEX_BIN, "-c", 'web_search="disabled"', "app-server", "--stdio"),
        approve_requests=False,
    ) as server:
        return server.request(
            "thread/read", {"threadId": thread_id, "includeTurns": True}, timeout=5
        )


def utcnow():
    return datetime.now(timezone.utc)


def origin_url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("a loopback HTTP origin without a path is required")
    if parsed.port is None:
        raise ValueError("an explicit dashboard port is required")
    return value


def preparation_script(config, request_id):
    binding = json.dumps(
        {"instance": config["extension_instance_id"], "profile": config["profile_name"]}
    )
    return f"""var {{setupBrowserRuntime}} = await import({json.dumps(str(BROWSER_CLIENT))});
var readinessAgent = await setupBrowserRuntime();
var readinessBinding = {binding};
var readinessMatches = (await readinessAgent.browsers.list()).filter(b => b.family === "chrome" && b.type === "extension" && b.profileName === readinessBinding.profile && b.metadata?.extensionInstanceId === readinessBinding.instance);
if (readinessMatches.length !== 1) throw new Error("readiness_browser_binding_unavailable");
var readinessBrowser = await readinessAgent.browsers.get(readinessMatches[0].id);
console.log(JSON.stringify({{readiness_prepared:{json.dumps(request_id)},browser_documentation:await readinessBrowser.documentation(),file_upload_documentation:await readinessAgent.documentation.get("file-uploads")}}));"""


def probe_script(config, request_id, fixture_url, pdf_path):
    return f"""await readinessBrowser.nameSession("🩺 Readiness");
var readinessTab = await readinessBrowser.tabs.new();
var readinessProof = {{request_id:{json.dumps(request_id)},browser:readinessMatches[0],tab_id:readinessTab.id,marker:null,file:null,closed:false}};
try {{
  await readinessTab.goto({json.dumps(fixture_url)});
  readinessProof.marker = await readinessTab.playwright.evaluate(() => ({{url:location.href,marker:document.getElementById("readiness-marker").textContent}}));
  var readinessChooserPending = readinessTab.playwright.waitForEvent("filechooser", {{timeoutMs:5000}});
  await readinessTab.playwright.getByLabel("Synthetic PDF").click();
  var readinessChooser = await readinessChooserPending;
  await readinessChooser.setFiles([{json.dumps(str(pdf_path))}]);
  readinessProof.file = await readinessTab.playwright.evaluate(() => {{var f=document.getElementById("readiness-file"); return {{input_type:f.type,name:f.value.replaceAll(String.fromCharCode(92),"/").split("/").pop()}};}});
}} finally {{
  await readinessTab.close();
  readinessProof.closed = !(await readinessBrowser.user.openTabs()).some(t => t.id === readinessTab.id);
  console.log(JSON.stringify(readinessProof));
}}"""


DISCOVERY_SCRIPT = 'text(ALL_TOOLS.filter(tool => tool.name === "mcp__node_repl__js"));'


def carrier_script(code, *, preparation=False):
    title = (
        "Read readiness browser documentation"
        if preparation
        else "Check the owned readiness fixture"
    )
    return (
        "const readinessResult = await tools.mcp__node_repl__js("
        + json.dumps({"code": code, "title": title}, separators=(",", ":"))
        + ");\nfor (const block of readinessResult.content ?? []) {\n"
        '  if (block.type === "text") text(block.text);\n}'
    )


def carrier_code(item):
    if item.get("type") not in {"functionCall", "dynamicToolCall"}:
        raise ValueError("unsupported_diagnostic_action")
    name = item.get("name", item.get("tool"))
    namespace = item.get("namespace")
    if not ((namespace == "functions" and name == "exec") or name == "functions.exec"):
        raise ValueError("unsupported_diagnostic_action")
    args = item.get("arguments", item.get("input"))
    if isinstance(args, dict):
        args = args.get("code")
    if not isinstance(args, str):
        raise ValueError("unsupported_diagnostic_action")
    return args


def diagnostic_actions(turn, preparation, proof):
    actions = []
    carriers = {
        DISCOVERY_SCRIPT,
        carrier_script(preparation, preparation=True),
        carrier_script(proof),
    }
    seen_carriers = set()
    for item in turn.get("items", []):
        kind = item.get("type")
        if kind in {
            "userMessage",
            "agentMessage",
            "reasoning",
            "plan",
            "contextCompaction",
        }:
            continue
        if kind == "functionCallOutput" and (
            item.get("namespace"),
            item.get("name"),
        ) == ("codex_app", "create_thread"):
            continue
        if kind in {"functionCall", "dynamicToolCall"}:
            code = carrier_code(item)
            if code not in carriers or code in seen_carriers:
                raise ValueError("unsupported_diagnostic_action")
            seen_carriers.add(code)
            if code == carrier_script(proof):
                actions.append(item)
            continue
        if (
            kind != "mcpToolCall"
            or item.get("server") not in {"node_repl", "mcp__node_repl"}
            or item.get("tool") != "js"
        ):
            raise ValueError("unsupported_diagnostic_action")
        actions.append(item)
    return actions


def tool_json(item):
    result = item.get("result")
    if not isinstance(result, dict) or result.get("isError") is True:
        return []
    values = []
    for block in result.get("content", []):
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        try:
            value = json.loads(block.get("text", ""))
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict) and isinstance(value.get("content"), list):
            values.extend(tool_json({"result": value}))
        else:
            values.append(value)
    return values


def verified_probe(turn, row, config, pdf_path):
    exact = probe_script(config, row["request_id"], row["url"], pdf_path)
    preparation = preparation_script(config, row["request_id"])
    actions = diagnostic_actions(turn, preparation, exact)
    if len(actions) == 1:
        prep = actions[0]
        args = prep.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = None
        if (
            prep.get("type") == "mcpToolCall"
            and prep.get("server") in {"node_repl", "mcp__node_repl"}
            and prep.get("tool") == "js"
            and isinstance(args, dict)
            and args.get("code") == preparation_script(config, row["request_id"])
        ):
            return {
                "page_state": "blocked",
                "extension": False,
                "cleanup": True,
                "upload": False,
            }, {
                "probe_version": 1,
                "preparation_only": True,
                "no_tab_created": True,
                "tool_item_id": prep.get("id"),
            }
    calls = []
    prepared = False
    for item in turn.get("items", []):
        if (
            item.get("type") != "mcpToolCall"
            or item.get("server") not in {"node_repl", "mcp__node_repl"}
            or item.get("tool") != "js"
        ):
            continue
        arguments = item.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = None
        if not isinstance(arguments, dict):
            raise ValueError("unsupported_probe_script")
        code = arguments.get("code")
        if (
            code == preparation_script(config, row["request_id"])
            and not prepared
            and not calls
        ):
            if item.get("status") != "completed" or item.get("error"):
                raise ValueError("preparation_incomplete")
            prepared = any(
                isinstance(v, dict)
                and v.get("readiness_prepared") == row["request_id"]
                and isinstance(v.get("browser_documentation"), str)
                and v["browser_documentation"]
                and isinstance(v.get("file_upload_documentation"), str)
                and v["file_upload_documentation"]
                for v in tool_json(item)
            )
            if not prepared:
                raise ValueError("preparation_unverified")
            continue
        if code != exact or not prepared:
            raise ValueError("unsupported_probe_script")
        if item.get("status") != "completed" or item.get("error"):
            raise ValueError("probe_tool_incomplete")
        for value in tool_json(item):
            if isinstance(value, dict) and value.get("request_id") == row["request_id"]:
                calls.append((item, value))
    if len(calls) != 1:
        raise ValueError("host_probe_unverified")
    item, value = calls[0]
    browser = value.get("browser", {})
    if (
        browser.get("family") != "chrome"
        or browser.get("type") != "extension"
        or browser.get("profileName") != config["profile_name"]
        or browser.get("metadata", {}).get("extensionInstanceId")
        != config["extension_instance_id"]
        or not isinstance(browser.get("id"), str)
        or not browser["id"]
    ):
        raise ValueError("browser_binding_mismatch")
    if value.get("marker") != {
        "url": row["url"],
        "marker": row["url"].rsplit("/", 1)[1],
    }:
        raise ValueError("fixture_marker_unverified")
    if (
        not isinstance(value.get("tab_id"), str)
        or not value["tab_id"]
        or value.get("closed") is not True
    ):
        raise ValueError("cleanup_unverified")
    expected_file = {"input_type": "file", "name": "readiness-fixture.pdf"}
    upload = value.get("file") == expected_file
    return {
        "page_state": "observed",
        "extension": True,
        "cleanup": True,
        "upload": upload,
    }, {
        "probe_version": 1,
        "tool_item_id": item.get("id"),
        "browser_binding_verified": True,
        "fixture_marker_verified": True,
        "owned_tab_closed": True,
        "synthetic_selection_verified": upload,
    }


def escaped_preparation_failure(data, row, config):
    target = row["candidate_target"]
    thread = desktop_thread(data, target, config["project_root"])
    turns = thread.get("turns", [])
    if len(turns) != 1:
        raise ValueError("diagnostic_has_additional_turns")
    items = turns[0].get("items", [])
    envelopes = [
        item for item in items
        if (item.get("type"), item.get("namespace"), item.get("name"))
        == ("functionCallOutput", "codex_app", "create_thread")
    ]
    if len(envelopes) != 1:
        raise ValueError("diagnostic_creation_envelope_unverified")
    output = envelopes[0].get("output")
    if isinstance(output, dict):
        output = output.get("text")
    bodies = re.findall(r"<input>([\s\S]*?)</input>", output or "")
    if len(bodies) != 1:
        raise ValueError("diagnostic_creation_envelope_unverified")
    delivered = unescape(bodies[0])
    marker = "Use these exact functions.exec carriers for preparation then proof. Do not run any other tools.\n"
    expected_prefix, expected_marker, _ = row["prompt"].partition(marker)
    actual_prefix, actual_marker, _ = delivered.partition(marker)
    if not expected_marker or actual_marker != expected_marker or actual_prefix != expected_prefix:
        raise ValueError("diagnostic_request_binding_unverified")
    turn = creation_turn(data, row | {"prompt": delivered}, config, target)
    inert = {"userMessage", "agentMessage", "reasoning", "plan", "contextCompaction"}
    actions = [item for item in items if item is not envelopes[0] and item.get("type") not in inert]
    if len(actions) != 1:
        raise ValueError("diagnostic_has_other_actions")
    action = actions[0]
    args = action.get("arguments")
    if isinstance(args, str):
        args = json.loads(args)
    result = action.get("result") or {}
    if (
        action.get("type") != "mcpToolCall"
        or action.get("server") not in {"node_repl", "mcp__node_repl"}
        or action.get("tool") != "js"
        or action.get("status") != "failed"
        or not isinstance(args, dict)
        or args.get("code") != json.dumps(preparation_script(config, row["request_id"]))[1:-1]
        or result.get("_meta", {}).get("codex/telemetry", {}).get("error_code") != "syntax_error"
        or not any(item.get("type") == "agentMessage" and item.get("phase") == "final_answer" for item in items)
    ):
        raise ValueError("preparation_compile_failure_unverified")
    return turn, action


class ReadinessBridge(InspectionBridge):
    policy = "readiness-only-v1"

    def __init__(self, root, **kwargs):
        kwargs.setdefault("reader", read_desktop_thread)
        super().__init__(root, "application-readiness", **kwargs)
        self.path = self.root / ".pipeline" / "application-readiness-bridge.sqlite3"
        if not self.path.resolve().is_relative_to(self.root):
            raise ValueError("diagnostic_state_outside_runtime")

    def register_readiness(
        self,
        *,
        project_id,
        relay,
        extension_instance_id,
        profile_name,
        origin,
        replace=False,
        project_root=None,
    ):
        project_root = project_directory(self.root, project_root)
        identifier(project_id, "project_id")
        identifier(relay, "relay")
        identifier(extension_instance_id, "extension_instance_id")
        if (
            not isinstance(profile_name, str)
            or not profile_name.strip()
            or len(profile_name) > 256
        ):
            raise ValueError("bounded browser profile name required")
        self.verify_relay(relay, project_root=project_root)
        config = {
            "policy": self.policy,
            "project_root": str(project_root),
            "project_id": project_id,
            "relay": relay,
            "extension_instance_id": extension_instance_id,
            "profile_name": profile_name,
            "origin": origin_url(origin),
        }
        encoded = json.dumps(config, sort_keys=True)
        with self.db(write=True) as db:
            prior = db.execute(
                "SELECT config FROM bridge_scopes WHERE scope=?", (self.scope,)
            ).fetchone()
            if prior and prior[0] != encoded:
                if not replace:
                    raise ValueError(
                        "use explicit register --replace only after original diagnostic reconciliation"
                    )
                self._rebind_safe(db)
                db.execute(
                    "UPDATE bridge_scopes SET config=? WHERE scope=?",
                    (encoded, self.scope),
                )
            else:
                db.execute(
                    "INSERT OR IGNORE INTO bridge_scopes VALUES(?,?)",
                    (self.scope, encoded),
                )
        return {"registered": True, "policy": self.policy}

    def _rebind_safe(self, db):
        for row in db.execute(
            "SELECT state,result FROM bridge_requests WHERE scope=?", (self.scope,)
        ):
            result = json.loads(row["result"]) if row["result"] else {}
            if (
                row["state"] not in {"completed", "blocked"}
                or result.get("cleanup") is not True
            ):
                raise ValueError(
                    "original diagnostic cleanup must be verified before replacing registration"
                )
        ledger = self.root / ".pipeline/application-readiness.sqlite3"
        if ledger.is_file():
            with closing(
                sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)
            ) as runs:
                if runs.execute(
                    "SELECT 1 FROM readiness_runs WHERE phase IN ('running','dispatch_unknown','cleanup_unknown') LIMIT 1"
                ).fetchone():
                    raise ValueError(
                        "active or unknown diagnostic prevents replacing registration"
                    )

    def verify_relay(self, relay, database=None, *, project_root=None):
        project_root = project_root or self.config()["project_root"]
        thread = desktop_thread(self.reader(relay), relay, project_root)
        title = thread.get("name", thread.get("title", ""))
        if title != "Application readiness relay" or any(
            t.get("status") not in {"completed", "failed", "interrupted", "cancelled"}
            for t in thread.get("turns", [])
        ):
            raise ValueError(
                "register an idle dedicated Desktop chat named Application readiness relay"
            )
        if database is not None and Path(database).is_file():
            with closing(
                sqlite3.connect(
                    Path(database).resolve().as_uri() + "?mode=ro", uri=True
                )
            ) as db:
                if db.execute(
                    "SELECT 1 FROM application_queue WHERE codex_application_session_id=? LIMIT 1",
                    (relay,),
                ).fetchone():
                    raise ValueError("application owner cannot be diagnostic relay")
        self._assert_dedicated(relay)

    def _assert_dedicated(self, relay):
        paths = [
            ("claude-batch-bridge.sqlite3", "scopes", "config"),
            ("blockers.sqlite3", "dq_scopes", "controller"),
        ]
        for filename, table, column in paths:
            path = self.root / ".pipeline" / filename
            if not path.is_file():
                continue
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                rows = db.execute(f'SELECT "{column}" FROM "{table}"').fetchall()
                for row in rows:
                    if (column == "controller" and row[0] == relay) or (
                        column == "config"
                        and json.loads(row[0]).get("bootstrap_relay") == relay
                    ):
                        raise ValueError(
                            "application coordinator cannot be a diagnostic relay"
                        )
        batch_db = self.root / ".pipeline/claude-batch-bridge.sqlite3"
        if batch_db.is_file():
            with closing(
                sqlite3.connect(batch_db.as_uri() + "?mode=ro", uri=True)
            ) as db:
                if db.execute(
                    "SELECT 1 FROM plans WHERE controller=? OR dispatcher=? LIMIT 1",
                    (relay, relay),
                ).fetchone():
                    raise ValueError(
                        "application coordinator cannot be diagnostic relay"
                    )
        for manifest in (self.root / ".pipeline/batches").glob("*.json"):
            data = json.loads(manifest.read_text())
            if any(
                data.get(key) == relay
                for key in (
                    "controller",
                    "controller_thread_id",
                    "dispatcher",
                    "dispatcher_thread_id",
                )
            ):
                raise ValueError("application coordinator cannot be a diagnostic relay")

    def validate_url(self, url, config):
        prefix = config["origin"] + "/api/apply-readiness/fixture/"
        if not url.startswith(prefix):
            raise ValueError("readiness fixture origin mismatch")
        suffix = url[len(prefix) :]
        if str(uuid.UUID(suffix)) != suffix:
            raise ValueError("readiness fixture identifier mismatch")

    def command(self, method, *arguments):
        return runtime_command(
            self.root,
            [
                sys.executable,
                "-m",
                "pipeline.application_readiness",
                "--root",
                str(self.root),
                method,
                *arguments,
            ]
        )

    def fixture_pdf(self, request_id):
        return (
            self.root
            / ".pipeline"
            / "readiness"
            / identifier(request_id, "request_id")
            / "readiness-fixture.pdf"
        )

    def prepare_request(self, request_id):
        pdf = self.fixture_pdf(request_id)
        if not pdf.resolve().is_relative_to(self.root):
            raise ValueError("diagnostic_artifact_outside_runtime")
        pdf.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with pdf.open("xb") as stream:
            stream.write(PDF)
        os.chmod(pdf, 0o600)

    def prompt(self, request_id, url):
        return (
            "Run only this harmless readiness diagnostic in this Codex Desktop chat. Do not open employer pages, "
            "applications, other tabs, personal files, accounts, grants, workers or chats. Use only the official node_repl "
            "js tool with the two exact scripts below, each once. Do not modify or replace its APIs, code or output. "
            "The script selects only the registered Chrome extension profile, creates one local fixture tab, "
            "selects one synthetic PDF without upload, then closes its own tab. Stop on a tool rejection; no fallback. "
            "Finish with a short factual result. Your final message is not diagnostic proof.\n"
            + "First run this exact preparation script, then read its browser and file-upload documentation.\n"
            + preparation_script(self.config(), request_id)
            + "\nAfter reading those documents run this exact proof script.\n"
            + probe_script(self.config(), request_id, url, self.fixture_pdf(request_id))
            + "\nIf node_repl is lazy-loaded, optional discovery through functions.exec must use exactly:\n"
            + DISCOVERY_SCRIPT
            + "\nUse these exact functions.exec carriers for preparation then proof. Do not run any other tools.\n"
            + carrier_script(
                preparation_script(self.config(), request_id), preparation=True
            )
            + "\n"
            + carrier_script(
                probe_script(
                    self.config(), request_id, url, self.fixture_pdf(request_id)
                )
            )
        )

    def verified_result(self, turn, row):
        return verified_probe(
            turn, row, self.config(), self.fixture_pdf(row["request_id"])
        )

    def reconcile(self, request_id):
        outcome = super().reconcile(request_id)
        if (
            outcome["state"] != "create_unknown"
            or outcome["target_thread"]
            or not outcome["candidate_target"]
            or outcome["cancelled"]
            or outcome["verification_error"] != "unique initial official create_thread provenance is unverified"
        ):
            return outcome
        row, config = self.request(request_id), self.config()
        try:
            data = self.reader(row["candidate_target"])
            turn, action = escaped_preparation_failure(data, row, config)
            lifecycle = self.terminal_reader(
                data["thread"].get("path"), row["candidate_target"],
                turn["id"], config["project_root"],
            )
            if lifecycle.get("state") != "completed":
                return outcome
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
            return outcome
        result = {"page_state": "blocked", "extension": False, "upload": False, "cleanup": True}
        evidence = {
            "creation_verified": False,
            "source_relay": config["relay"],
            "turn_id": turn["id"],
            "turn_lifecycle": lifecycle,
            "tool_item_id": action.get("id"),
            "preparation_syntax_error": True,
            "no_tab_created": True,
        }
        with self.db(write=True) as db:
            db.execute(
                "UPDATE bridge_requests SET state='blocked',result=?,host_evidence=?,revision=revision+1 "
                "WHERE request_id=? AND scope=? AND state='create_unknown' AND candidate_target=? "
                "AND target_thread IS NULL AND cancelled=0 AND revision=?",
                (json.dumps(result), json.dumps(evidence), request_id, self.scope,
                 row["candidate_target"], row["revision"]),
            )
        return self.public(self.request(request_id))

    def cancel(self, request_id):
        raise ValueError("readiness diagnostics require original-owner reconciliation")

    def relay_next(self, relay):
        action = super().relay_next(relay)
        if action["status"] != "official_create_required":
            return action
        path = self.fixture_pdf(action["request_id"]).parent / "create-thread.json"
        with path.open("x") as stream:
            json.dump(action["arguments"], stream)
        path.chmod(0o600)
        command = {"cmd": "cat " + shlex.quote(str(path)), "max_output_tokens": 20000}
        config = self.config()
        action["creation_instructions"] = (
            "Run create_carrier verbatim through functions.exec exactly once. "
            "It reads the frozen arguments and invokes the official create_thread tool. "
            "Do not retype or re-escape arguments.prompt. Then acknowledge the returned "
            "threadId with after_send. An unknown carrier result requires reconciliation, "
            "never another creation."
        )
        action["create_carrier"] = (
            "const projectsResult = await tools.mcp__codex_app__list_projects({});\n"
            "const projects = JSON.parse(projectsResult.content.find(b => b.type === 'text').text).projects;\n"
            "if (!projects.some(p => p.projectId === "
            + json.dumps(config["project_id"])
            + " && p.path === "
            + json.dumps(config["project_root"])
            + ")) throw new Error('Registered project root changed');\n"
            "const frozen = await tools.exec_command("
            + json.dumps(command)
            + ");\n"
            "if (frozen.exit_code !== 0 || frozen.session_id) throw new Error('Frozen creation arguments unavailable');\n"
            "text(await tools.mcp__codex_app__create_thread(JSON.parse(frozen.output)));"
        )
        return action


LABELS = {
    "runtime_root": "Application runtime root",
    "profile": "Application profile",
    "resumes": "Resume catalog and PDFs",
    "python": "Python and pipeline imports",
    "execution_python": "Application worker Python",
    "node": "Node runtime",
    "autofill": "Autofill module",
    "jobfeed_cli": "Jobfeed Python CLI",
    "execution_cli": "Application Jobfeed executable",
    "jobfeed_db": "Jobfeed database schema",
    "artifacts": "Diagnostic artifact writing",
    "codex_cli": "Codex CLI",
    "desktop": "Codex Desktop",
    "browser_runtime": "Official Chrome browser runtime",
    "registration": "Diagnostic relay and browser binding",
    "harness": "Codex Desktop diagnostic execution",
    "chrome": "Chrome extension connection",
    "upload": "Synthetic PDF file chooser",
    "cleanup": "Owned diagnostic tab cleanup",
    "coordination": "Batch coordination and gates",
    "monitor": "Batch progress monitor",
    "site": "Employer login, CAPTCHA and upload acceptance",
}


def check(key, status, reason, now, *, required=True, next_step=""):
    return {
        "id": key,
        "label": LABELS[key],
        "status": status,
        "required": required,
        "checked_at": now.isoformat(),
        "reason": reason,
        "next_step": next_step,
    }


def succeeds(argv, *, cwd, env=None):
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=8,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def coordination_check(root, now):
    path = root / ".pipeline/coordination.sqlite3"
    if not path.is_file():
        return check(
            "coordination",
            "not_tested",
            "Batch-specific coordination is initialized when a batch starts. No gates or grants were created.",
            now,
            required=False,
        )
    try:
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as db:
            unresolved = db.execute(
                "SELECT COUNT(*) FROM coordination_claims WHERE status='needs_reconciliation'"
            ).fetchone()[0]
            tabs = db.execute(
                "SELECT COUNT(*) FROM coordination_tabs WHERE state='needs_reconciliation'"
            ).fetchone()[0]
        if unresolved or tabs:
            return check(
                "coordination",
                "blocked",
                "Existing resources need original-owner reconciliation before a diagnostic. No claims were released.",
                now,
                required=True,
            )
        return check(
            "coordination",
            "ready",
            "Coordination schemas are readable and no resources need reconciliation.",
            now,
            required=False,
        )
    except (OSError, sqlite3.Error):
        return check(
            "coordination",
            "blocked",
            "Existing coordination state is unreadable. Reconcile it before a diagnostic.",
            now,
            required=True,
        )


def local_checks(root, database, python, now):
    checks = []
    present = root.is_dir() and (root / "pipeline/cli.py").is_file()
    checks.append(
        check(
            "runtime_root",
            "ready" if present else "blocked",
            "Configured application source is available."
            if present
            else "Configured application source is missing.",
            now,
        )
    )
    profile = root / "application-profile.md"
    try:
        profile_ready = profile.is_file() and bool(profile.read_text().strip())
    except (OSError, UnicodeError):
        profile_ready = False
    checks.append(
        check(
            "profile",
            "ready" if profile_ready else "blocked",
            "Nonempty profile is available."
            if profile_ready
            else "Add a nonempty application-profile.md in the application runtime.",
            now,
        )
    )
    try:
        from pipeline.resume_catalog import ResumeCatalog

        count = len(ResumeCatalog.load(root / "resume-catalog.json").entries)
        checks.append(
            check(
                "resumes", "ready", f"{count} existing resume entries validated.", now
            )
        )
    except (OSError, ValueError, TypeError, KeyError):
        checks.append(
            check(
                "resumes",
                "blocked",
                "Resume catalog or a referenced PDF is missing or invalid.",
                now,
                next_step="Validate resume-catalog.json and its referenced PDF and facts files locally.",
            )
        )
    env = dict(os.environ, PYTHONPATH=str(root))
    imports = [
        str(python),
        "-c",
        'import pipeline.cli, pipeline.application_readiness, jobfeed; print("ready")',
    ]
    python_ok = present and succeeds(imports, cwd=root, env=env)
    checks.append(
        check(
            "python",
            "ready" if python_ok else "blocked",
            "Configured diagnostic Python imports pipeline and Jobfeed."
            if python_ok
            else "Configured diagnostic Python cannot import required modules.",
            now,
        )
    )
    worker_python = root / ".venv/bin/python"
    worker_ok = present and succeeds(
        [str(worker_python), "-c", "import pipeline.cli, jobfeed"], cwd=root, env=env
    )
    checks.append(
        check(
            "execution_python",
            "ready" if worker_ok else "blocked",
            "Application worker Python imports required modules."
            if worker_ok
            else "Application workers require the project .venv/bin/python and its dependencies.",
            now,
        )
    )
    node = shutil.which("node")
    node_ok = bool(node and present and succeeds([node, "--version"], cwd=root))
    checks.append(
        check(
            "node",
            "ready" if node_ok else "blocked",
            "Node runs."
            if node_ok
            else "Install Node and expose it to the dashboard process.",
            now,
        )
    )
    component = root / "autofill/component.mjs"
    autofill_ok = bool(
        node_ok
        and component.is_file()
        and succeeds(
            [
                node,
                "--input-type=module",
                "-e",
                'const m=await import(process.argv[1]); if(!["autofillPage","applyAutofillAnswers","verifyAutofillPage"].every(k=>typeof m[k]==="function"))process.exit(2)',
                component.as_uri(),
            ],
            cwd=root,
        )
    )
    checks.append(
        check(
            "autofill",
            "ready" if autofill_ok else "blocked",
            "Official Autofill exports load locally."
            if autofill_ok
            else "Autofill module could not load with the configured Node runtime.",
            now,
        )
    )
    cli_ok = present and succeeds(
        [str(python), "-c", 'from jobfeed.cli import cli; cli(["--help"])'],
        cwd=root,
        env=env,
    )
    checks.append(
        check(
            "jobfeed_cli",
            "ready" if cli_ok else "blocked",
            "Formal Jobfeed CLI loads."
            if cli_ok
            else "Install Jobfeed in the configured Python runtime.",
            now,
        )
    )
    execution_cli = root.parent / "jobfeed/.venv/bin/jobfeed"
    execution_ok = present and succeeds([str(execution_cli), "--help"], cwd=root)
    checks.append(
        check(
            "execution_cli",
            "ready" if execution_ok else "blocked",
            "Application Jobfeed executable runs."
            if execution_ok
            else "Application batches require the sibling Jobfeed .venv/bin/jobfeed executable.",
            now,
        )
    )
    schema_ok = False
    try:
        with closing(
            sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        ) as db:
            required = {
                "jobs": {"id", "real_job_id", "company", "title", "jd_text", "url"},
                "applied": {"job_id"},
                "application_queue": {
                    "job_id",
                    "state",
                    "retry_count",
                    "next_retry_at",
                    "application_folder_relpath",
                    "codex_application_session_id",
                    "submit_authorization",
                },
            }
            schema_ok = all(
                columns
                <= {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
                for table, columns in required.items()
            )
    except (OSError, sqlite3.Error):
        pass
    checks.append(
        check(
            "jobfeed_db",
            "ready" if schema_ok else "blocked",
            "Configured Jobfeed database schema is readable."
            if schema_ok
            else "Configured Jobfeed database is missing, unreadable or requires migration.",
            now,
        )
    )
    for key, path in [
        ("codex_cli", Path(CODEX_BIN)),
        ("desktop", Path("/Applications/ChatGPT.app/Contents/Info.plist")),
        ("browser_runtime", BROWSER_CLIENT),
    ]:
        ok = path.is_file() and (
            key != "codex_cli" or succeeds([str(path), "--version"], cwd=root)
        )
        checks.append(
            check(
                key,
                "ready" if ok else "blocked",
                "Required local component is present."
                if ok
                else "Install or configure this required local component.",
                now,
            )
        )
    checks.append(coordination_check(root, now))
    checks.append(
        check(
            "monitor",
            "not_tested",
            "Progress monitoring is batch-specific. This check does not start a worker or monitor.",
            now,
            required=False,
        )
    )
    checks.append(
        check(
            "site",
            "not_tested",
            "Login, CAPTCHA, permissions and employer file acceptance must be checked within each authorized job.",
            now,
            required=False,
        )
    )
    return checks


class ApplicationReadiness:
    def __init__(self, root, database, *, python=None, bridge=None, clock=None):
        self.root = Path(root).resolve()
        self.database = Path(database).resolve()
        self.python = Path(python or sys.executable)
        self.bridge = bridge or ReadinessBridge(root)
        self.clock = clock or utcnow
        self.path = self.root / ".pipeline" / "application-readiness.sqlite3"

    def _db(self):
        if not self.root.is_dir() or not (self.root / "pipeline/cli.py").is_file():
            raise ValueError("configured_application_root_unavailable")
        if not self.path.resolve().is_relative_to(self.root):
            raise ValueError("diagnostic_state_outside_runtime")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=10)
        os.chmod(self.path, 0o600)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute(
            "CREATE TABLE IF NOT EXISTS readiness_runs(run_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, phase TEXT NOT NULL, binding TEXT NOT NULL, result TEXT NOT NULL, bridge_request TEXT)"
        )
        db.commit()
        return db

    def _binding(self):
        try:
            config = self.bridge.config()
        except (OSError, ValueError, sqlite3.Error):
            config = None
        files = [
            self.root / "application-profile.md",
            self.root / "resume-catalog.json",
            self.root / "autofill/component.mjs",
            self.root / "pipeline/application_readiness.py",
            BROWSER_CLIENT,
            self.python,
            self.root / ".venv/bin/python",
            self.root.parent / "jobfeed/.venv/bin/jobfeed",
            Path(CODEX_BIN),
            Path("/Applications/ChatGPT.app/Contents/Info.plist"),
        ]
        if shutil.which("node"):
            files.append(Path(shutil.which("node")))
        try:
            catalog = json.loads((self.root / "resume-catalog.json").read_text())
            for entry in catalog.get("resumes", []):
                for key in ("pdf", "autofill", "text"):
                    if entry.get("available") is True and isinstance(
                        entry.get(key), str
                    ):
                        path = Path(entry[key])
                        files.append(path if path.is_absolute() else self.root / path)
        except (OSError, ValueError, AttributeError, TypeError):
            pass
        try:
            stat = self.database.stat()
            with closing(
                sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)
            ) as db:
                schema_version = db.execute("PRAGMA schema_version").fetchone()[0]
            database_identity = [stat.st_dev, stat.st_ino, schema_version]
        except (OSError, sqlite3.Error):
            database_identity = None
        identities = []
        for path in files:
            try:
                stat = path.stat()
                identities.append(
                    [
                        str(path),
                        stat.st_dev,
                        stat.st_ino,
                        stat.st_size,
                        stat.st_mtime_ns,
                        stat.st_ctime_ns,
                    ]
                )
            except OSError:
                identities.append([str(path), None])
        return json.dumps(
            {
                "root": str(self.root),
                "database": str(self.database),
                "database_identity": database_identity,
                "python": str(self.python),
                "config": config,
                "files": identities,
            },
            sort_keys=True,
        )

    def _public(self, data, binding):
        now = self.clock()
        started = datetime.fromisoformat(data["started_at"])
        stale = binding != self._binding() or started > now + timedelta(seconds=30)
        if data.get("expires_at"):
            stale = stale or now >= datetime.fromisoformat(data["expires_at"])
        data["stale"] = stale
        data["ready"] = (
            not stale
            and data["phase"] == "completed"
            and all(c["status"] == "ready" for c in data["checks"] if c["required"])
        )
        return data

    def start(self, request_id, origin):
        request_id = str(uuid.UUID(request_id))
        origin_url(origin)
        now = self.clock()
        with closing(self._db()) as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT * FROM readiness_runs WHERE request_id=? OR phase IN ('running','dispatch_unknown','cleanup_unknown') ORDER BY rowid DESC LIMIT 1",
                (request_id,),
            ).fetchone()
            if prior:
                db.rollback()
                return self.get(prior["run_id"])
            run_id = str(uuid.uuid4())
            checks = local_checks(self.root, self.database, self.python, now)
            config = None
            try:
                config = self.bridge.config()
                self.bridge.verify_relay(config["relay"], self.database)
                if config["origin"] != origin:
                    raise ValueError("dashboard_origin_mismatch")
                checks.append(
                    check(
                        "registration",
                        "ready",
                        "Dedicated diagnostic relay and browser binding verified.",
                        now,
                    )
                )
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                RuntimeError,
                sqlite3.Error,
            ):
                config = None
                checks.append(
                    check(
                        "registration",
                        "blocked",
                        "Dedicated diagnostic relay or browser binding is not configured for this dashboard.",
                        now,
                        next_step='python -m pipeline.application_readiness --root "$APPLICATION_ROOT" register --project-root "$CODEX_PROJECT_ROOT" --project-id "$CODEX_PROJECT_ID" --relay "$READINESS_RELAY_ID" --extension-instance-id "$EXTENSION_INSTANCE_ID" --profile-name "$CHROME_PROFILE_NAME" --origin "$DASHBOARD_ORIGIN"',
                    )
                )
            directory = self.root / ".pipeline/readiness" / run_id
            try:
                if not directory.resolve().is_relative_to(self.root):
                    raise OSError("diagnostic_artifact_outside_runtime")
                directory.mkdir(parents=True, exist_ok=False, mode=0o700)
                marker = directory / "write-check"
                marker.write_text(run_id)
                artifact_ok = marker.read_text() == run_id
                marker.unlink()
            except OSError:
                artifact_ok = False
            checks.append(
                check(
                    "artifacts",
                    "ready" if artifact_ok else "blocked",
                    "Diagnostic artifact writing and removal verified."
                    if artifact_ok
                    else "Diagnostic artifact directory is not writable.",
                    now,
                )
            )
            for key in ("harness", "chrome", "upload", "cleanup"):
                checks.append(
                    check(
                        key,
                        "not_tested",
                        "Awaiting the dedicated harmless Desktop diagnostic."
                        if config
                        else "Configure the dedicated diagnostic relay first.",
                        now,
                    )
                )
            coordination_blocked = any(
                c["id"] == "coordination" and c["status"] == "blocked" for c in checks
            )
            phase = (
                "running"
                if config and artifact_ok and not coordination_blocked
                else "blocked"
            )
            data = {
                "run_id": run_id,
                "phase": phase,
                "started_at": now.isoformat(),
                "finished_at": self.clock().isoformat() if phase == "blocked" else None,
                "expires_at": (now + timedelta(seconds=TTL_SECONDS)).isoformat()
                if phase == "blocked"
                else None,
                "checks": checks,
                "ready": False,
                "stale": False,
            }
            binding = self._binding()
            db.execute(
                "INSERT INTO readiness_runs VALUES(?,?,?,?,?,NULL)",
                (run_id, request_id, phase, binding, json.dumps(data)),
            )
            db.commit()
        if phase == "running":
            try:
                result = self.bridge.submit(
                    origin + "/api/apply-readiness/fixture/" + run_id, run_id
                )
                with closing(self._db()) as db:
                    db.execute(
                        "UPDATE readiness_runs SET bridge_request=? WHERE run_id=?",
                        (result["request_id"], run_id),
                    )
                    db.commit()
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                data["phase"] = "dispatch_unknown"
                self._save(data)
        return self.get(run_id)

    def _save(self, data):
        with closing(self._db()) as db:
            db.execute(
                "UPDATE readiness_runs SET phase=?,result=? WHERE run_id=? AND phase IN ('running','dispatch_unknown','cleanup_unknown')",
                (data["phase"], json.dumps(data), data["run_id"]),
            )
            db.commit()

    def get(self, run_id=None):
        if not self.path.is_file():
            return None
        with closing(self._db()) as db:
            row = (
                db.execute(
                    "SELECT * FROM readiness_runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if run_id
                else db.execute(
                    "SELECT * FROM readiness_runs ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
            )
        if row is None:
            return None
        data = json.loads(row["result"])
        if (
            not row["bridge_request"]
            and data["phase"] in ACTIVE
            and self.bridge.path.is_file()
        ):
            with self.bridge.db() as bridge_db:
                recovered = bridge_db.execute(
                    "SELECT request_id FROM bridge_requests WHERE scope=? AND idempotency_key=?",
                    (self.bridge.scope, data["run_id"]),
                ).fetchone()
            if recovered:
                with closing(self._db()) as db:
                    db.execute(
                        "UPDATE readiness_runs SET bridge_request=? WHERE run_id=? AND bridge_request IS NULL",
                        (recovered[0], data["run_id"]),
                    )
                    db.commit()
                return self.get(data["run_id"])
            data["phase"] = "dispatch_unknown"
            self._save(data)
        if row["bridge_request"] and data["phase"] in ACTIVE:
            result = self.bridge.get(row["bridge_request"])
            now = self.clock()
            verified = result["state"] == "completed" and result.get("result")
            if verified:
                proofs = {
                    "harness": True,
                    "chrome": verified["extension"],
                    "upload": verified["upload"],
                    "cleanup": verified["cleanup"],
                }
                data["checks"] = [
                    check(
                        c["id"],
                        "ready" if proofs[c["id"]] else "not_tested",
                        "Host-verified harmless diagnostic passed."
                        if proofs[c["id"]]
                        else "Synthetic chooser selection was not verified.",
                        now,
                    )
                    if c["id"] in proofs
                    else c
                    for c in data["checks"]
                ]
                data["phase"] = "completed"
                data["finished_at"] = now.isoformat()
                data["expires_at"] = (
                    datetime.fromisoformat(data["started_at"])
                    + timedelta(seconds=TTL_SECONDS)
                ).isoformat()
            elif (
                result["state"] == "blocked"
                and (result.get("result") or {}).get("cleanup") is True
            ):
                data["phase"] = "blocked"
                syntax_failure = (result.get("host_evidence") or {}).get("preparation_syntax_error") is True
                statuses = {
                    "harness": "blocked" if syntax_failure else "ready",
                    "chrome": "not_tested" if syntax_failure else "blocked",
                    "upload": "not_tested",
                    "cleanup": "ready",
                }
                data["checks"] = [
                    check(
                        c["id"],
                        statuses[c["id"]],
                        "Diagnostic preparation failed to parse before execution. No browser tab was created. Retry the repaired diagnostic."
                        if syntax_failure else
                        "Host verified that preparation created no browser tab. Configure the bound Chrome extension and try again.",
                        now,
                    )
                    if c["id"] in statuses
                    else c
                    for c in data["checks"]
                ]
                data["finished_at"] = now.isoformat()
            elif result["state"] in {"failed", "blocked", "cancelled"}:
                data["phase"] = "cleanup_unknown"
                data["checks"] = [
                    check(
                        c["id"],
                        "not_tested",
                        "Diagnostic did not establish a complete host proof. Reconcile the original diagnostic chat before another check.",
                        now,
                    )
                    if c["id"] in {"harness", "chrome", "upload", "cleanup"}
                    else c
                    for c in data["checks"]
                ]
            elif result["wake_state"] == "wake_unknown" or result["state"] in {
                "create_unknown",
                "receipt_pending",
            }:
                data["phase"] = "dispatch_unknown"
            self._save(data)
        with closing(self._db()) as db:
            current = db.execute(
                "SELECT result,binding FROM readiness_runs WHERE run_id=?",
                (data["run_id"],),
            ).fetchone()
        return self._public(json.loads(current["result"]), current["binding"])

    def fixture(self, run_id):
        run_id = str(uuid.UUID(run_id))
        if not self.path.is_file():
            raise ValueError("unknown diagnostic fixture")
        with closing(self._db()) as db:
            if not db.execute(
                "SELECT 1 FROM readiness_runs WHERE run_id=?", (run_id,)
            ).fetchone():
                raise ValueError("unknown diagnostic fixture")
        return (
            '<!doctype html><html><head><meta charset="utf-8"><title>Application readiness fixture</title></head>'
            "<body><h1>Harmless application readiness check</h1><p>No application or upload is performed.</p>"
            f'<p id="readiness-marker">{run_id}</p><label for="readiness-file">Synthetic PDF</label>'
            '<input id="readiness-file" type="file" accept="application/pdf"></body></html>'
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--database", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register")
    register.add_argument("--replace", action="store_true")
    register.add_argument("--project-root", type=Path)
    for name in (
        "project-id",
        "relay",
        "extension-instance-id",
        "profile-name",
        "origin",
    ):
        register.add_argument("--" + name, required=True)
    next_cmd = commands.add_parser("relay-next")
    next_cmd.add_argument("--relay", required=True)
    ack = commands.add_parser("relay-ack")
    ack.add_argument("request_id")
    ack.add_argument("--relay", required=True)
    ack.add_argument("--target", required=True)
    show = commands.add_parser("relay-reconcile")
    show.add_argument("request_id")
    show.add_argument("--relay", required=True)
    start = commands.add_parser("start")
    start.add_argument("--request-id", required=True)
    start.add_argument("--origin", required=True)
    commands.add_parser("get")
    fixture = commands.add_parser("fixture")
    fixture.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    bridge = ReadinessBridge(args.root)
    try:
        if args.command in {"start", "get", "fixture"}:
            if args.database is None:
                raise ValueError("configured Jobfeed database required")
            coordinator = ApplicationReadiness(args.root, args.database)
            if args.command == "start":
                result = coordinator.start(args.request_id, args.origin)
            elif args.command == "fixture":
                result = coordinator.fixture(args.run_id)
            else:
                result = coordinator.get()
        elif args.command == "register":
            result = bridge.register_readiness(
                project_id=args.project_id,
                relay=args.relay,
                extension_instance_id=args.extension_instance_id,
                profile_name=args.profile_name,
                origin=args.origin,
                replace=args.replace,
                project_root=args.project_root,
            )
        elif args.command == "relay-next":
            result = bridge.relay_next(args.relay)
        elif args.command == "relay-ack":
            result = bridge.acknowledge(args.request_id, args.relay, args.target)
        else:
            bridge.authorize_relay(args.relay)
            result = bridge.reconcile(args.request_id)
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        print(
            json.dumps({"error": "readiness_setup_or_reconciliation_failed"}),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
