"""Readable chat labels derived from the frozen job selection."""


def _text(value):
    return " ".join(value.split())[:160] if isinstance(value, str) else ""


def slot_letter(index):
    label = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        label = chr(65 + remainder) + label
    return label


def controller_title(jobs):
    companies = list(dict.fromkeys(_text(job.get("company")) for job in jobs if isinstance(job, dict)))
    companies = [company for company in companies if company]
    if not companies:
        return "Controller · Applications"
    summary = ", ".join(companies[:2])
    if len(companies) > 2:
        summary += f" and {len(companies) - 2} more"
    return f"Controller · {summary} applications"[:160]


def applier_title(manifest, job):
    candidates = manifest.get("ordered_candidates", [])
    matches = [(index, candidate) for index, candidate in enumerate(candidates)
               if isinstance(candidate, dict) and candidate.get("id") == job.id] if isinstance(candidates, list) else []
    label, candidate = "Applier", {}
    if len(matches) == 1:
        index, candidate = matches[0]
        label += " " + slot_letter(index)
    company = _text(candidate.get("company")) or _text(job.company)
    title = _text(candidate.get("title")) or _text(job.title)
    return " · ".join(part for part in (label, company, title) if part)[:160]
