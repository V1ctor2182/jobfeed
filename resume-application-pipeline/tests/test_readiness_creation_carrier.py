import json
from pathlib import Path
import subprocess

from test_application_readiness import ReadinessFixture


class ReadinessCreationCarrierTests(ReadinessFixture):
    def action(self):
        self.start()
        return self.bridge.relay_next("relay")

    def execute(self, action, project_root=None):
        payload = {
            "carrier": action["create_carrier"],
            "projects": [{"projectId": "project", "path": str(project_root or self.root)}],
            "arguments": action["arguments"],
        }
        script = """import fs from 'node:fs';
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
let created = [];
const tools = {
  mcp__codex_app__list_projects: async () => ({content:[{type:'text',text:JSON.stringify({projects:input.projects})}]}),
  exec_command: async () => ({exit_code:0,output:JSON.stringify(input.arguments)}),
  mcp__codex_app__create_thread: async args => {created.push(args);return {threadId:'created'};}
};
let error = null;
try { await new Function('tools','text','return (async () => {' + input.carrier + '})()')(tools, () => {}); }
catch (e) { error = e.message; }
console.log(JSON.stringify({created,error}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            input=json.dumps(payload), text=True, capture_output=True, check=True,
        )
        return json.loads(result.stdout)

    def test_frozen_arguments_preserve_nested_scripts_exactly(self):
        action = self.action()
        path = self.bridge.fixture_pdf(action["request_id"]).parent / "create-thread.json"
        self.assertEqual(json.loads(path.read_text()), action["arguments"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        result = self.execute(action)
        self.assertIsNone(result["error"])
        self.assertEqual(result["created"], [action["arguments"]])
        self.assertEqual(self.bridge.relay_next("relay")["status"], "idle")

    def test_changed_primary_folder_prevents_creation(self):
        result = self.execute(self.action(), Path("/unrelated"))
        self.assertEqual(result["created"], [])
        self.assertEqual(result["error"], "Registered project root changed")
