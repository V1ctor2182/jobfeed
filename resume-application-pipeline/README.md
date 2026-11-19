# Resume Application Pipeline

Local application workflow using existing resumes, Claude CLI Appliers, batch orchestration, and verified results. Each Applier chooses an existing PDF from the batch's frozen catalog. No resume-authoring session is required.

## Included

| Component | Location |
|---|---|
| Existing resume catalog and selection | `pipeline/resume_catalog.py` |
| Claude Applier launch, tools and supervision | `pipeline/claude_applier.py`, `pipeline/applier_mcp.py`, `pipeline/applier_supervisor.py` |
| Field helpers for hard widgets | `applier_helpers/` |
| Authorization rules and terminology | `AGENTS.md`, `GLOSSARY.md` |
| Batch orchestration and state machines | `pipeline/` |
| Resume generation and validation tools | `tools/` |
| Synthetic class templates | `bases/` |
| Historical resume authoring utilities | `.claude/skills/tailor-resume/` |
| Regression tests | `tests/` |

See [Pipeline coordination reference](docs/coordination.md) for durable job tabs and shared account claims.

See [Autopilot](../docs/autopilot.md) for the standing authorization, which only the user turns on, that lets a continuous Claude batch apply to eligible jobs.

See [Existing resume selection](docs/existing-resumes.md) for the private PDF catalog, the Applier's per-job choice, and selected-file provenance.

For this combined repository, use the root [offline test instructions](../README.md#run-the-offline-tests) to include the URL-intake dependencies and all Python tests.

## Local setup

Python 3.11+ and the `claude` CLI are required. List existing PDFs in the private `resume-catalog.json` and supply confirmed profile facts. The application path does not require Microsoft Word or document generation.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pipeline.cli --help
```

Copy the example application profile to its matching local root name and configure the resume catalog with your existing files. Supply only confirmed facts. The examples contain no authorization to apply or submit. Local catalogs, profiles, applications, and runtime state are ignored by Git. Do not force-add them.

The old CONTENT converters and synthetic authoring fixtures remain for historical compatibility tests. New applications do not generate CONTENT, DOCX, or a tailored resume.

## External integrations

This is not a self-contained hosted service. Jobfeed is the sibling folder `../jobfeed`, with its own `data/jobfeed.sqlite`, API and official apply CLI. `JOBFEED_API_URL` configures its API endpoint.

Read AGENTS.md and docs/pipeline.md before starting a real batch. Batch scope, concurrency and submit permission must come from current user authorization. No worker is started by setup or tests.

## Offline validation

```sh
.venv/bin/python tools/create_example_template.py
PYTHONPATH=.:tests .venv/bin/python -m pytest tests -q
```

Some historical regression cases depend on private source fixtures or external projects. Browser submission, Word rendering and production end-to-end delivery are not implied by offline tests.

## Privacy and third-party material

See PRIVACY.md and export-manifest.json. Historical personal resumes, profile facts, passwords, account answers, screenshots, receipts, logs, local settings and Git commits are excluded. Like the rest of this repository, the pipeline is released under the [MIT License](../LICENSE).

## Setup prompt

Use [SETUP_PROMPT.md](SETUP_PROMPT.md) for installation and offline verification of the existing-resume workflow. Setup does not authorize real applications.
