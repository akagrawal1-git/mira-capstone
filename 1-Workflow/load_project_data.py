"""
Load Project Data - Langflow Custom Component for Mira.

Reads one project's files fresh on every call and returns computed facts
(counts, dates, blocked/overdue tasks, upcoming milestones, allowed IDs),
so the specialist agents never count or do date math themselves.

Folder layout (one folder per project_id under base_path):
    <base_path>/<project_id>/description.txt
    <base_path>/<project_id>/timeline.csv     columns: phase,phase_name,start_week,end_week,key_activities,milestones_deliverables
    <base_path>/<project_id>/risks.csv        columns: risk_id,category,risk_challenge,impact,mitigation_strategy
    <base_path>/<project_id>/task_board.csv   columns: task_id,task_name,status,assignee,priority,sprint,due_date,labels,description
    <base_path>/<project_id>/project.json     optional: {"project_name", "week1_start", "default_as_of"}

Any file may be missing; the output then says which data is unavailable
(this is what lets Mira answer T6 "no task data" honestly).
"""

from lfx.custom.custom_component.component import Component
from lfx.io import MessageTextInput, Output, StrInput
from lfx.schema.data import Data

import csv
import io
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

DEFAULT_AS_OF = "2026-04-24"       # GUESS until the course gives a "today" date
DEFAULT_WEEK1_START = "2026-03-30"  # GUESS: Monday of project week 1
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


# ---------------------------- pure logic -----------------------------------
def _read_csv(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig")
    return [{k.strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(io.StringIO(text))]


def _d(s: str) -> date:
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def compute_facts(project_dir: Path, project_id: str, as_of: str | None = None) -> dict:
    cfg = {}
    if (project_dir / "project.json").exists():
        cfg = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    as_of = (as_of or "").strip() or cfg.get("default_as_of") or DEFAULT_AS_OF
    today = _d(as_of)
    week1 = _d(cfg.get("week1_start", DEFAULT_WEEK1_START))

    facts: dict = {
        "project_id": project_id,
        "project_name": cfg.get("project_name", project_id),
        "as_of": as_of,
        "available_data": {},
    }

    # Description
    desc_path = project_dir / "description.txt"
    facts["available_data"]["description"] = desc_path.exists()
    facts["description"] = desc_path.read_text(encoding="utf-8").strip() if desc_path.exists() else None

    # Timeline -> phases with real dates
    phases = []
    if (project_dir / "timeline.csv").exists():
        for p in _read_csv(project_dir / "timeline.csv"):
            sw, ew = int(p["start_week"]), int(p["end_week"])
            phases.append({
                "phase": int(p["phase"]),
                "name": p["phase_name"],
                "start_week": sw,
                "end_week": ew,
                "start_date": (week1 + timedelta(weeks=sw - 1)).isoformat(),
                "end_date": (week1 + timedelta(days=ew * 7 - 1)).isoformat(),
                "key_activities": [a.strip() for a in p["key_activities"].split(";") if a.strip()],
                "milestones": [m.strip() for m in p["milestones_deliverables"].split(";") if m.strip()],
            })
    facts["available_data"]["timeline"] = bool(phases)
    facts["phases"] = phases
    week = (today - week1).days // 7 + 1
    facts["project_week"] = week
    cur = next((p for p in phases if p["start_week"] <= week <= p["end_week"]), None)
    facts["current_phase"] = cur["name"] if cur else None
    facts["milestones_next_14_days"] = [
        {"phase": p["phase"], "phase_name": p["name"], "due": p["end_date"], "milestones": p["milestones"]}
        for p in phases if today <= _d(p["end_date"]) <= today + timedelta(days=14)
    ]
    nxt = [p for p in phases if _d(p["end_date"]) > today + timedelta(days=14)]
    facts["next_milestone_after_window"] = (
        {"phase": nxt[0]["phase"], "phase_name": nxt[0]["name"], "due": nxt[0]["end_date"], "milestones": nxt[0]["milestones"]}
        if nxt else None
    )

    # Risks
    risks = _read_csv(project_dir / "risks.csv") if (project_dir / "risks.csv").exists() else []
    facts["available_data"]["risks"] = bool(risks)
    facts["risks"] = risks

    # Task board
    tasks = _read_csv(project_dir / "task_board.csv") if (project_dir / "task_board.csv").exists() else []
    facts["available_data"]["task_board"] = bool(tasks)
    counts: dict[str, int] = {}
    by_sprint: dict[str, dict] = {}
    for t in tasks:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
        s = by_sprint.setdefault(t["sprint"], {"task_ids": [], "counts": {}})
        s["task_ids"].append(t["task_id"])
        s["counts"][t["status"]] = s["counts"].get(t["status"], 0) + 1
    facts["task_counts"] = {"total": len(tasks), **counts}
    facts["by_sprint"] = by_sprint

    open_tasks = [t for t in tasks if t["status"] != "Done"]
    brief = lambda t: {k: t[k] for k in ("task_id", "task_name", "status", "assignee", "priority", "sprint", "due_date")}
    facts["blocked_tasks"] = [
        {**brief(t), "blocked_reason": t["description"].split("BLOCKED:", 1)[1].strip() if "BLOCKED:" in t["description"] else ""}
        for t in open_tasks if t["status"] == "Blocked"
    ]
    facts["overdue_tasks"] = [
        {**brief(t), "days_overdue": (today - _d(t["due_date"])).days}
        for t in open_tasks if _d(t["due_date"]) < today
    ]
    facts["due_next_7_days"] = [
        brief(t) for t in open_tasks if today <= _d(t["due_date"]) <= today + timedelta(days=7)
    ]
    facts["tasks"] = tasks
    facts["allowed_ids"] = {
        "tasks": [t["task_id"] for t in tasks],
        "risks": [r["risk_id"] for r in risks],
        "phases": [p["phase"] for p in phases],
    }
    facts["rules"] = (
        "Use these counts and dates exactly. Cite task_id / risk_id / phase for every fact. "
        "If a needed item in available_data is false, say that data is not available; do not guess."
    )
    return facts


def load_project(base_path: str, project_id: str, as_of: str | None = None) -> dict:
    pid = (project_id or "").strip().lower()
    base = Path(base_path).expanduser()
    known = sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
    if not ID_PATTERN.match(pid) or pid not in known:
        return {
            "project_id": pid,
            "error": "no_data_for_project",
            "message": f"No project files found for project_id '{pid}'. Do not invent project data; "
                       f"ask the user for a description, timeline, risks or task board.",
            "known_project_ids": known,
        }
    return compute_facts(base / pid, pid, as_of)


# ---------------------------- Langflow component ---------------------------
class LoadProjectData(Component):
    display_name = "Load Project Data"
    description = (
        "Returns computed facts for one project_id: task counts by status and sprint, blocked, "
        "overdue and due-soon tasks, upcoming milestones with dates, the risk register, and the "
        "task and risk IDs that may be cited. Always call this before answering; never count or "
        "compute dates yourself."
    )
    icon = "folder-open"
    name = "LoadProjectData"

    inputs = [
        MessageTextInput(
            name="project_id",
            display_name="Project ID",
            info="Folder name of the project, e.g. abcde-ai-adoption.",
            value="abcde-ai-adoption",
            tool_mode=True,
        ),
        MessageTextInput(
            name="as_of_date",
            display_name="As-of date (YYYY-MM-DD)",
            info="Date to treat as 'today'. Leave empty to use the project's default.",
            value="",
            required=False,
            tool_mode=True,
        ),
        StrInput(
            name="base_path",
            display_name="Projects folder",
            info="Absolute path of the folder that holds one sub-folder per project_id.",
            value="",  # set in the Langflow UI, e.g. C:\\path\\to\\data\\projects
        ),
    ]

    outputs = [
        Output(display_name="Project facts", name="facts", method="build_facts"),
    ]

    def build_facts(self) -> Data:
        facts = load_project(self.base_path, self.project_id, self.as_of_date)
        self.status = facts.get("error") or (
            f"{facts['project_id']} as of {facts['as_of']}: {facts['task_counts']}"
        )
        return Data(data=facts)


if __name__ == "__main__":  # local test (needs lfx installed, or stub it): python load_project_data.py <projects dir> [project_id] [as_of]
    import sys
    out = load_project(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "abcde-ai-adoption",
                       sys.argv[3] if len(sys.argv) > 3 else None)
    out.pop("tasks", None); out.pop("risks", None); out.pop("description", None); out.pop("phases", None)
    print(json.dumps(out, indent=1))
