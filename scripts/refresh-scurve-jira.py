#!/usr/bin/env python3
"""Fetch Jira S-curve data and update canvas + sidecar + HTML export."""

from __future__ import annotations

import argparse
import gzip
import http.client
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

TOKEN = os.environ.get("JIRA_TOKEN", "")
BASE = "https://task.enco.ru/rest/api/2"


def read_json(path: Path):
    raw = path.read_bytes()
    for enc in ("utf-8", "cp1251"):
        try:
            return json.loads(raw.decode(enc))
        except UnicodeDecodeError:
            continue
    return json.loads(raw.decode("utf-8", errors="replace"))


def write_json(path: Path, data, *, indent: int | None = None) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=indent),
        encoding="utf-8",
    )


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

FIELDS = (
    "summary,issuetype,status,created,customfield_10506,customfield_12100,"
    "customfield_10501,customfield_10502,customfield_10503,customfield_10504,"
    "customfield_11901,customfield_11902"
)
# Children under Epic: epic link + Parent-Child («Порождает»)
CHILD_FIELDS = (
    "summary,issuetype,status,issuelinks,"
    "customfield_10506,customfield_12100,customfield_10503,customfield_10504,"
    "customfield_11901,customfield_11902"
)
JQL_TEMPLATE = (
    'project = {proj} AND (issuetype = Epic OR '
    '(issuetype = Задача AND "Ссылка на эпик" is EMPTY))'
)
LINK_TYPE_PARENT_CHILD = "Parent-Child"
CHILD_ISSUE_TYPES = {"История", "Задача", "Story", "Task"}
MILESTONE_TYPES = {"Задача", "Task"}
PROJECT_LABELS = {
    "KKP": "Казань Клубная ГП-16.004",
    "KK": "Казань Клубная ГП-16.001–003",
    "H2O": "Н. Новгород H2O",
    "NN3S": "Н. Новгород Три стихии",
    "AMSP": "Екатеринбург Соната-Парк",
    "AV6": "Тюмень Айва 4",
    "AV12": "Тюмень Айва 12-13",
    "AV14": "Тюмень Айва 5 ГП-14п",
    "AV15": "Тюмень Айва 6 ГП-15",
    "AV9": "Тюмень Айва 9, 11",
    "AV7": "Тюмень Айва 7",
    "PR6": "Тюмень ПРЕО 6",
    "PREO211": "Тюмень ПРЕО 7 ГП-211,212",
    "PREO213": "Тюмень ПРЕО 7 ГП-213",
    "PREO8": "Тюмень ПРЕО 8",
    "MS": "Тюмень Мириады 1",
    "MS103": "Тюмень Мириады 1 ГП-103.4",
    "MS108": "Тюмень Мириады 1 ГП-108",
    "MS2": "Тюмень Мириады 2",
    "MS3": "Тюмень Мириады 3",
    "BR1": "Тюмень Беринг 1",
    "LB2": "Тюмень Беринг 2",
    "LB3": "Тюмень Беринг 3.1",
    "B32": "Тюмень Беринг 3.2",
    "LB4": "Тюмень Беринг УДС 3 этап",
    "NU81": "Н. Уренгой Преображенский 1",
    "NU82": "Н. Уренгой Преображенский 2",
}
ALL_PROJECTS = list(PROJECT_LABELS.keys())


def projects_banner() -> str:
    return f"{len(ALL_PROJECTS)} проектов"


def project_has_cache(proj: str) -> bool:
    cache_dir = CACHE_ROOT / proj
    if not cache_dir.is_dir():
        return False
    return any(p.suffix == ".json" and not p.name.startswith("_") for p in cache_dir.iterdir())

ROOT = Path(__file__).resolve().parents[1]
CANVAS = ROOT / "canvases" / "s-curve.canvas.tsx"
SIDECAR = CANVAS.with_name(CANVAS.name.replace(".tsx", ".data.json"))
HTML = ROOT / "s-curve.html"
EXPORTS = ROOT / "exports"
CACHE_ROOT = Path("/tmp/amsp_cache_projects")


def _read_json_response(resp) -> dict:
    raw = resp.read()
    encoding = (resp.headers.get("Content-Encoding") or "").lower()
    if "gzip" in encoding:
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def api(path: str, *, timeout: int = 180, retries: int = 5) -> dict:
    if not TOKEN:
        raise SystemExit("JIRA_TOKEN is not set")
    req = urllib.request.Request(
        BASE + path,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    last_err: Exception | None = None
    retryable_read = (
        socket.timeout,
        TimeoutError,
        http.client.IncompleteRead,
        ConnectionError,
        OSError,
    )
    attempts = max(retries, 1)
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _read_json_response(resp)
        except urllib.error.HTTPError as err:
            last_err = err
            if err.code not in (429, 502, 503, 504) or attempt == attempts:
                raise
            wait = min(8 * attempt, 30)
            print(
                f"  retry {attempt}/{attempts} after HTTP {err.code} (sleep {wait}s)",
                file=sys.stderr,
            )
            time.sleep(wait)
        except retryable_read as err:
            last_err = err
            if attempt == attempts:
                raise
            wait = min(8 * attempt, 30)
            print(
                f"  retry {attempt}/{attempts} after {type(err).__name__}: {err} (sleep {wait}s)",
                file=sys.stderr,
            )
            time.sleep(wait)
    raise last_err or SystemExit("Jira API failed")


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    if len(s) >= 5 and s[-5] in "+-" and s[-3] != ":":
        s = s[:-2] + ":" + s[-2:]
    return datetime.fromisoformat(s)


def parse_num(s) -> float | None:
    if s is None or s == "":
        return None
    try:
        return float(str(s).replace(",", ".").replace("%", "").strip())
    except ValueError:
        return None


def parse_date_val(v) -> str | None:
    if not v:
        return None
    s = str(v).strip()[:10]
    return s if re.match(r"^\d{4}-\d{2}-\d{2}$", s) else None


def plan_dates_from_fields(f: dict) -> dict:
    """Эффективные плановые даты: перенос (11901/11902) приоритетнее базового плана."""
    start_orig = parse_date_val(f.get("customfield_10503"))
    end_orig = parse_date_val(f.get("customfield_10504"))
    start_shift = parse_date_val(f.get("customfield_11901"))
    end_shift = parse_date_val(f.get("customfield_11902"))
    return {
        "start_plan": start_shift or start_orig,
        "end_plan": end_shift or end_orig,
        "start_plan_orig": start_orig,
        "end_plan_orig": end_orig,
        "plan_rescheduled": bool(start_shift or end_shift),
    }


def events_from_changelog(raw: dict) -> list[dict]:
    events = []
    for h in raw.get("changelog", {}).get("histories", []):
        for it in h.get("items", []):
            if it.get("field") in ("% факт", "% план"):
                events.append(
                    {
                        "at": h["created"],
                        "field": "fact" if it["field"] == "% факт" else "plan",
                        "to": parse_num(it.get("toString")),
                    }
                )
    return events


def issue_fields_payload(f: dict) -> dict:
    return {
        "summary": f["summary"],
        "issuetype": f["issuetype"]["name"],
        "status": f["status"]["name"],
        "created": f["created"],
        "customfield_10506": f.get("customfield_10506"),
        "customfield_12100": f.get("customfield_12100"),
        "customfield_10501": f.get("customfield_10501"),
        "customfield_10502": f.get("customfield_10502"),
        "customfield_10503": f.get("customfield_10503"),
        "customfield_10504": f.get("customfield_10504"),
        "customfield_11901": f.get("customfield_11901"),
        "customfield_11902": f.get("customfield_11902"),
    }


def fetch_issue(key: str, cache_dir: Path) -> dict:
    cache_path = cache_dir / f"{key}.json"
    if cache_path.exists():
        try:
            return read_json(cache_path)
        except (UnicodeDecodeError, json.JSONDecodeError, OSError):
            cache_path.unlink(missing_ok=True)
    print(f"  issue {key}...")
    try:
        raw = api(
            f"/issue/{key}?expand=changelog&fields={FIELDS}",
            timeout=180,
            retries=3,
        )
        f = raw["fields"]
        events = events_from_changelog(raw)
    except (socket.timeout, TimeoutError, http.client.IncompleteRead, OSError) as err:
        print(
            f"  {key}: changelog failed ({type(err).__name__}), fields only",
            file=sys.stderr,
        )
        raw = api(f"/issue/{key}?fields={FIELDS}", timeout=60, retries=3)
        f = raw["fields"]
        events = []
        created = f.get("created")
        if created:
            plan_now = parse_num(f.get("customfield_12100"))
            fact_now = parse_num(f.get("customfield_10506"))
            if plan_now:
                events.append({"at": created, "field": "plan", "to": 0.0})
                events.append(
                    {
                        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "field": "plan",
                        "to": plan_now,
                    }
                )
            if fact_now:
                events.append({"at": created, "field": "fact", "to": 0.0})
                events.append(
                    {
                        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "field": "fact",
                        "to": fact_now,
                    }
                )
    data = {
        "key": key,
        "fields": issue_fields_payload(f),
        "events": events,
    }
    write_json(cache_path, data)
    return data


def series_from_issue_data(data: dict) -> dict:
    f = data["fields"]
    events = sorted(data["events"], key=lambda e: e["at"])
    dates = plan_dates_from_fields(f)
    return {
        "summary": f["summary"],
        "type": f["issuetype"],
        "status": f["status"],
        "created": parse_dt(f["created"]),
        "plan_now": f.get("customfield_12100"),
        "fact_now": f.get("customfield_10506"),
        "start_fact": f.get("customfield_10501"),
        "end_fact": f.get("customfield_10502"),
        "events": events,
        **dates,
    }


def load_series(proj: str, force: bool = False) -> dict:
    cache_dir = CACHE_ROOT / proj
    cache_dir.mkdir(parents=True, exist_ok=True)
    if force:
        for p in cache_dir.glob("*.json"):
            p.unlink()
        hier_dir = cache_dir / "_hier"
        if hier_dir.exists():
            for p in hier_dir.glob("*.json"):
                p.unlink()
    enc = urllib.parse.quote(JQL_TEMPLATE.format(proj=proj))
    search = api(f"/search?jql={enc}&maxResults=50&fields={FIELDS}")
    series = {}
    for meta in search["issues"]:
        key = meta["key"]
        data = fetch_issue(key, cache_dir)
        series[key] = series_from_issue_data(data)
    return series


def load_series_from_cache(proj: str) -> dict:
    """Build series from local issue cache only (no Jira network)."""
    cache_dir = CACHE_ROOT / proj
    if not cache_dir.exists():
        raise SystemExit(f"No cache for {proj} at {cache_dir}")
    series = {}
    for path in sorted(cache_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue
        data = read_json(path)
        series[data["key"]] = series_from_issue_data(data)
    if not series:
        raise SystemExit(f"Empty cache for {proj}")
    return series


def search_all(jql: str, fields: str, page: int = 100) -> list[dict]:
    start = 0
    out: list[dict] = []
    while True:
        enc = urllib.parse.quote(jql)
        raw = api(
            f"/search?jql={enc}&startAt={start}&maxResults={page}&fields={fields}"
        )
        batch = raw.get("issues") or []
        out.extend(batch)
        start += len(batch)
        if start >= raw.get("total", 0) or not batch:
            break
    return out


def child_node_from_fields(key: str, f: dict) -> dict:
    plan = f.get("customfield_12100")
    fact = f.get("customfield_10506")
    if isinstance(plan, str):
        plan = parse_num(plan)
    if isinstance(fact, str):
        fact = parse_num(fact)
    lag = round((fact or 0) - (plan or 0), 1)
    dates = plan_dates_from_fields(f)
    node = {
        "key": key,
        "summary": f.get("summary") or "",
        "type": (f.get("issuetype") or {}).get("name")
        if isinstance(f.get("issuetype"), dict)
        else (f.get("issuetype") or ""),
        "status": (f.get("status") or {}).get("name")
        if isinstance(f.get("status"), dict)
        else (f.get("status") or ""),
        "plan": plan,
        "fact": fact,
        "lag": lag,
        "period": f"{dates['start_plan'] or '—'} → {dates['end_plan'] or '—'}",
        "children": [],
    }
    if dates["plan_rescheduled"]:
        node["planRescheduled"] = True
        node["periodOrig"] = (
            f"{dates['start_plan_orig'] or '—'} → {dates['end_plan_orig'] or '—'}"
        )
    return node


def outward_porozhdaet_keys(issuelinks: list | None) -> list[str]:
    keys: list[str] = []
    for link in issuelinks or []:
        t = link.get("type") or {}
        if t.get("name") != LINK_TYPE_PARENT_CHILD:
            continue
        outward = link.get("outwardIssue")
        if outward and outward.get("key"):
            keys.append(outward["key"])
    return keys


def build_porozhdaet_tree(nodes: dict[str, dict], edges: dict[str, list[str]]) -> list[dict]:
    """Build forest from Parent-Child («Порождает») edges among epic children."""
    child_of: set[str] = set()
    for parent, kids in edges.items():
        for k in kids:
            if k in nodes:
                child_of.add(k)

    def type_rank(t: str) -> int:
        if t in ("История", "Story"):
            return 0
        if t in ("Задача", "Task"):
            return 1
        return 2

    def walk(key: str, stack: set[str]) -> dict:
        node = dict(nodes[key])
        if key in stack:
            node["children"] = []
            return node
        stack = stack | {key}
        kids = []
        for ck in edges.get(key, []):
            if ck in nodes:
                kids.append(walk(ck, stack))
        kids.sort(key=lambda n: (type_rank(n["type"]), n["key"]))
        node["children"] = kids
        return node

    roots = [k for k in nodes if k not in child_of]
    roots.sort(key=lambda k: (type_rank(nodes[k]["type"]), nodes[k]["key"]))
    return [walk(k, set()) for k in roots]


def fetch_epic_hierarchy(epic_key: str, cache_dir: Path, force: bool = False) -> list[dict]:
    """
    Epic → Истории/Задачи («Ссылка на эпик»), дальше дерево по исходящим «Порождает».
    """
    hier_dir = cache_dir / "_hier"
    hier_dir.mkdir(parents=True, exist_ok=True)
    cache_path = hier_dir / f"{epic_key}.json"
    if cache_path.exists() and not force:
        try:
            return read_json(cache_path)
        except (UnicodeDecodeError, json.JSONDecodeError, OSError):
            cache_path.unlink(missing_ok=True)

    jql = (
        f'"Ссылка на эпик" = {epic_key} AND issuetype in (История, Задача)'
    )
    try:
        raw_issues = search_all(jql, CHILD_FIELDS)
    except Exception as e:
        print(f"  hierarchy fetch failed for {epic_key}: {e}", file=sys.stderr)
        return []

    nodes: dict[str, dict] = {}
    edges: dict[str, list[str]] = {}
    link_stubs: dict[str, dict] = {}

    for issue in raw_issues:
        key = issue["key"]
        f = issue["fields"]
        itype = (f.get("issuetype") or {}).get("name", "")
        if itype not in CHILD_ISSUE_TYPES:
            continue
        nodes[key] = child_node_from_fields(key, f)
        out_keys = outward_porozhdaet_keys(f.get("issuelinks"))
        edges[key] = out_keys
        for link in f.get("issuelinks") or []:
            t = link.get("type") or {}
            if t.get("name") != LINK_TYPE_PARENT_CHILD:
                continue
            outward = link.get("outwardIssue")
            if not outward:
                continue
            ok = outward["key"]
            of = outward.get("fields") or {}
            if ok not in nodes and ok not in link_stubs:
                # Stub from link payload (may lack % план/% факт)
                link_stubs[ok] = child_node_from_fields(
                    ok,
                    {
                        "summary": of.get("summary"),
                        "issuetype": of.get("issuetype"),
                        "status": of.get("status"),
                        "customfield_12100": None,
                        "customfield_10506": None,
                        "customfield_10503": None,
                        "customfield_10504": None,
                        "customfield_11901": None,
                        "customfield_11902": None,
                    },
                )

    # Promote stubs that are Порождает-targets but missing epic-link (edge case)
    for k, stub in link_stubs.items():
        if k not in nodes and stub["type"] in CHILD_ISSUE_TYPES | {""}:
            if not stub["type"]:
                stub["type"] = "Задача"
            nodes[k] = stub

    tree = build_porozhdaet_tree(nodes, edges)
    write_json(cache_path, tree)
    return tree


def load_hierarchy_cached(epic_key: str, cache_dir: Path) -> list[dict]:
    path = cache_dir / "_hier" / f"{epic_key}.json"
    if path.exists():
        try:
            return read_json(path)
        except (UnicodeDecodeError, json.JSONDecodeError, OSError):
            path.unlink(missing_ok=True)
    return []


def attach_hierarchies(
    issue_series: dict, cache_dir: Path, force: bool = False, network: bool = True
) -> dict[str, list[dict]]:
    """Return map package_key → hierarchy tree (Epics only)."""
    out: dict[str, list[dict]] = {}
    for key, iss in issue_series.items():
        if iss["type"] != "Epic":
            out[key] = []
            continue
        if network:
            print(f"  hierarchy {key}...")
            out[key] = fetch_epic_hierarchy(key, cache_dir, force=force)
        else:
            out[key] = load_hierarchy_cached(key, cache_dir)
        n = count_hierarchy_nodes(out[key])
        if n:
            print(f"    {key}: {n} узлов (Истории/Задачи)")
    return out


def count_hierarchy_nodes(nodes: list[dict]) -> int:
    n = 0
    for node in nodes:
        n += 1 + count_hierarchy_nodes(node.get("children") or [])
    return n


def duration_days(iss: dict) -> int:
    sp, ep = iss["start_plan"], iss["end_plan"]
    if sp and ep:
        return max((date.fromisoformat(ep) - date.fromisoformat(sp)).days, 1)
    return 1


def month_ends(d0: date, d1: date) -> list[date]:
    out: list[date] = []
    y, m = d0.year, d0.month
    while True:
        me = date(y, 12, 31) if m == 12 else date(y, m + 1, 1) - timedelta(days=1)
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        if me >= d0:
            if me > d1:
                if not out or out[-1] != d1:
                    out.append(d1)
                break
            out.append(me)
        y, m = ny, nm
    return out


def hist_value(iss: dict, field: str, on_date: date) -> float:
    val = 0.0
    for e in iss["events"]:
        if e["field"] != field:
            continue
        at = parse_dt(e["at"]).date()
        if at <= on_date:
            if e["to"] is not None:
                val = e["to"]
        else:
            break
    return val


def schedule_plan(iss: dict, on_date: date) -> float:
    sp, ep = iss["start_plan"], iss["end_plan"]
    if not sp or not ep:
        return 0.0
    d0, d1 = date.fromisoformat(sp), date.fromisoformat(ep)
    if on_date < d0:
        return 0.0
    if on_date >= d1:
        return 100.0
    return 100.0 * (on_date - d0).days / max((d1 - d0).days, 1)


def num_or_none(v) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return parse_num(v)


def forecast_end_date(iss: dict, today: date) -> str | None:
    """Ожидаемая дата окончания вехи: закрытая → факт; иначе растяжка остатка работ."""
    fact = num_or_none(iss.get("fact_now")) or 0.0
    plan = num_or_none(iss.get("plan_now"))
    if plan is None:
        plan = schedule_plan(iss, today)
    end_plan = iss.get("end_plan")
    end_fact = iss.get("end_fact")
    if fact >= 99.9:
        return end_fact or end_plan
    if not end_plan:
        return end_fact
    end = date.fromisoformat(end_plan)
    start_s = iss.get("start_plan")
    start = date.fromisoformat(start_s) if start_s else None
    rem_work = max(100.0 - fact, 0.0) / 100.0
    if rem_work <= 0:
        return today.isoformat()
    rem_plan_pct = max(100.0 - plan, 0.01) / 100.0
    if today >= end or plan >= 99.9:
        dur = max((end - start).days, 1) if start else 30
        return (today + timedelta(days=max(round(dur * rem_work), 1))).isoformat()
    rem_days = max((end - today).days, 1)
    stretch = rem_work / rem_plan_pct
    return (today + timedelta(days=round(rem_days * stretch))).isoformat()


def build_milestones(issue_series: dict, today: date) -> list[dict]:
    out: list[dict] = []
    for k in sort_issue_keys(list(issue_series.keys())):
        iss = issue_series[k]
        if iss.get("type") not in MILESTONE_TYPES:
            continue
        fact = num_or_none(iss.get("fact_now")) or 0.0
        status = (iss.get("status") or "").lower()
        closed = fact >= 99.9 or status in {"закрыт", "done", "closed"}
        out.append(
            {
                "key": k,
                "summary": iss.get("summary") or "",
                "endPlan": iss.get("end_plan"),
                "endFact": (iss.get("end_fact") or iss.get("end_plan")) if closed else None,
                "endForecast": forecast_end_date(iss, today),
            }
        )
    return out


def milestones_from_project_dict(p: dict, today: date) -> list[dict]:
    """Вехи из уже собранного ProjectData (issues + views), без Jira."""
    views = p.get("views") or {}
    series: dict = {}
    for row in p.get("issues") or []:
        if row.get("type") not in MILESTONE_TYPES:
            continue
        key = row["key"]
        v = views.get(key) or {}
        start_plan = v.get("start_plan")
        end_plan = v.get("end_plan")
        period = row.get("period") or ""
        if (not start_plan or not end_plan) and "→" in period:
            left, right = [s.strip() for s in period.split("→", 1)]
            if not start_plan and re.match(r"^\d{4}-\d{2}-\d{2}$", left):
                start_plan = left
            if not end_plan and re.match(r"^\d{4}-\d{2}-\d{2}$", right):
                end_plan = right
        series[key] = {
            "summary": row.get("summary") or v.get("summary") or "",
            "type": row.get("type"),
            "status": row.get("status") or v.get("status") or "",
            "plan_now": v.get("plan_now") if v.get("plan_now") is not None else row.get("plan"),
            "fact_now": v.get("fact_now") if v.get("fact_now") is not None else row.get("fact"),
            "start_plan": start_plan,
            "end_plan": end_plan,
            "end_fact": v.get("end_fact"),
        }
    return build_milestones(series, today)


def forecast_linear_issue(iss: dict, d: date, today: date) -> float:
    if d <= today:
        return hist_value(iss, "fact", d)
    fact_now = iss["fact_now"] if iss["fact_now"] is not None else 0.0
    if fact_now >= 99.9:
        return 100.0
    ep = iss["end_fact"] or iss["end_plan"]
    if not ep:
        return fact_now
    end = date.fromisoformat(ep)
    if d >= end:
        return 100.0
    rem = max((end - today).days, 1)
    t = min(max((d - today).days / rem, 0), 1)
    return fact_now + (100.0 - fact_now) * t


def r1(v):
    return None if v is None else round(v, 1)


def issue_key_num(key: str) -> int:
    m = re.search(r"-(\d+)$", key)
    return int(m.group(1)) if m else 0


def sort_issue_keys(keys: list[str]) -> list[str]:
    return sorted(keys, key=lambda k: (issue_key_num(k), k))


def build_project(
    proj: str,
    issue_series: dict,
    today: date,
    updated_at: str,
    hierarchies: dict[str, list[dict]] | None = None,
) -> dict:
    if not issue_series:
        return build_empty_project(proj, today, updated_at)
    weights = {k: duration_days(v) for k, v in issue_series.items()}
    w_total = sum(weights.values()) or 1
    starts, ends = [], []
    for iss in issue_series.values():
        for d in [iss["start_plan"], iss["start_fact"]]:
            if d:
                starts.append(date.fromisoformat(d))
        if iss["created"]:
            starts.append(iss["created"].date())
        for d in [iss["end_plan"], iss["end_fact"]]:
            if d:
                ends.append(date.fromisoformat(d))
    proj_start, proj_end = min(starts), max(ends)
    if proj_end < today:
        proj_end = today

    months = month_ends(proj_start, proj_end)
    if today not in months and proj_start <= today <= proj_end:
        months = sorted(set(months + [today]))

    chart_dates: list[date] = []
    for d in months:
        if d in (today, proj_end) or d >= date(2025, 1, 1) or d.month in (3, 6, 9, 12):
            chart_dates.append(d)
    if not chart_dates:
        chart_dates = months

    def label(d: date) -> str:
        return d.strftime("%d.%m.%y") if d == today else d.strftime("%m.%y")

    categories = [label(d) for d in chart_dates]
    point_dates = [d.isoformat() for d in chart_dates]

    def build_for_keys(keys: list[str], meta: dict) -> dict:
        sub = {k: issue_series[k] for k in keys}
        sw = {k: weights[k] for k in keys}
        sw_total = sum(sw.values()) or 1

        def agg(on_date: date, getter):
            return round(sum(getter(iss) * sw[k] for k, iss in sub.items()) / sw_total, 2)

        p_track = next(
            (d for d in months if agg(d, lambda iss: hist_value(iss, "plan", d)) > 5),
            None,
        )
        f_track = next(
            (d for d in months if agg(d, lambda iss: hist_value(iss, "fact", d)) > 1),
            None,
        )

        first_plan = first_fact = None
        if len(keys) == 1:
            iss0 = sub[keys[0]]
            for e in iss0["events"]:
                if e["to"] is None:
                    continue
                at = parse_dt(e["at"]).date()
                if e["field"] == "plan" and e["to"] > 0 and first_plan is None:
                    first_plan = at
                if e["field"] == "fact" and e["to"] > 0 and first_fact is None:
                    first_fact = at

        st = agg(today, lambda iss: schedule_plan(iss, today))
        pt = agg(today, lambda iss: hist_value(iss, "plan", today))
        ft = agg(today, lambda iss: hist_value(iss, "fact", today))
        lag = ft - pt
        denom = max(100 - st, 0.01)

        plan_a, fact_a, lin, planlag, cal = [], [], [], [], []
        for d in chart_dates:
            plan_hist = agg(d, lambda iss: hist_value(iss, "plan", d))
            fact_hist = agg(d, lambda iss: hist_value(iss, "fact", d))
            plan_sched = agg(d, lambda iss: schedule_plan(iss, d))

            if d <= today:
                if len(keys) == 1 and first_plan:
                    plan = plan_hist if d >= first_plan else plan_sched
                else:
                    plan = plan_hist if (p_track and d >= p_track) else plan_sched

                if len(keys) == 1:
                    iss0 = sub[keys[0]]
                    if first_fact and d >= first_fact:
                        fact = fact_hist
                    elif (iss0["fact_now"] or 0) >= 99.9:
                        ep = date.fromisoformat(iss0["end_fact"] or iss0["end_plan"])
                        sp = date.fromisoformat(iss0["start_fact"] or iss0["start_plan"])
                        if d >= ep:
                            fact = 100.0
                        elif d >= sp:
                            fact = fact_hist
                        else:
                            fact = None
                    else:
                        fact = None
                        sp = iss0["start_plan"]
                        if (
                            sp
                            and d >= date.fromisoformat(sp)
                            and (iss0["fact_now"] or 0) == 0
                            and not first_fact
                        ):
                            fact = 0.0
                else:
                    fact = fact_hist if (f_track and d >= f_track) else None
                f_lin = f_pl = f_cal = fact
            else:
                t = (plan_sched - st) / denom
                plan = round(min(100, max(0, pt + (100 - pt) * t)), 2)
                fact = None
                f_lin = agg(d, lambda iss: forecast_linear_issue(iss, d, today))
                f_pl = round(min(100, max(0, plan + lag)), 2)
                tcal = (plan_sched - st) / denom
                f_cal = round(min(100, max(0, ft + (100 - ft) * tcal)), 2)

            plan_a.append(r1(plan))
            fact_a.append(r1(fact))
            lin.append(r1(f_lin))
            planlag.append(r1(f_pl))
            cal.append(r1(f_cal))

        return {
            **meta,
            "fact_today": round(ft, 1),
            "plan_today": round(pt, 1),
            "sched_today": round(st, 1),
            "lag": round(lag, 1),
            "lag_vs_sched": round(ft - st, 1),
            "plan": plan_a,
            "fact": fact_a,
            "forecastLinear": lin,
            "forecastPlanLag": planlag,
            "forecastCalendar": cal,
        }

    n_done = sum(1 for i in issue_series.values() if (i["fact_now"] or 0) >= 99.9)
    n_open = len(issue_series) - n_done
    n_rescheduled = sum(
        1 for i in issue_series.values() if i.get("plan_rescheduled")
    )

    views = {}
    views["ALL"] = build_for_keys(
        list(issue_series.keys()),
        {
            "key": "ALL",
            "summary": f"Весь проект {proj}",
            "type": "Агрегат",
            "status": f"{n_done} закрыто / {n_open} в работе",
            "start_plan": proj_start.isoformat(),
            "end_plan": proj_end.isoformat(),
            "start_plan_orig": None,
            "end_plan_orig": None,
            "plan_rescheduled": False,
            "end_fact": proj_end.isoformat(),
            "weight": w_total,
            "plan_now": None,
            "fact_now": None,
        },
    )
    views["ALL"]["plan_now"] = views["ALL"]["plan_today"]
    views["ALL"]["fact_now"] = views["ALL"]["fact_today"]

    for k in sorted(issue_series.keys(), key=lambda x: -weights[x]):
        iss = issue_series[k]
        views[k] = build_for_keys(
            [k],
            {
                "key": k,
                "summary": iss["summary"],
                "type": iss["type"],
                "status": iss["status"],
                "start_plan": iss["start_plan"],
                "end_plan": iss["end_plan"],
                "start_plan_orig": iss.get("start_plan_orig"),
                "end_plan_orig": iss.get("end_plan_orig"),
                "plan_rescheduled": iss.get("plan_rescheduled", False),
                "end_fact": iss["end_fact"],
                "weight": weights[k],
                "plan_now": iss["plan_now"],
                "fact_now": iss["fact_now"],
            },
        )

    options = [{"value": "ALL", "label": f"Весь проект {proj} ({len(issue_series)} этапов)"}] + [
        {
            "value": k,
            "label": f"{k} · {issue_series[k]['type']} · {issue_series[k]['summary']}",
        }
        for k in sort_issue_keys(list(issue_series.keys()))
    ]

    issues_table = []
    hierarchies = hierarchies or {}
    for k in sort_issue_keys(list(issue_series.keys())):
        v = issue_series[k]
        lag = round((v["fact_now"] or 0) - (v["plan_now"] or 0), 1)
        if (v["fact_now"] or 0) >= 99.9:
            tone = "success"
        elif lag < -1:
            tone = "danger"
        elif lag > 1:
            tone = "info"
        else:
            tone = "neutral"
        hier = hierarchies.get(k) or []
        row = {
                "key": k,
                "summary": v["summary"],
                "type": v["type"],
                "status": v["status"],
                "plan": v["plan_now"],
                "fact": v["fact_now"],
                "lag": lag,
                "period": f"{v['start_plan'] or '—'} → {v['end_plan'] or '—'}",
                "weight": weights[k],
                "tone": tone,
                "hierarchy": hier,
                "hierarchyCount": count_hierarchy_nodes(hier),
            }
        if v.get("plan_rescheduled"):
            row["planRescheduled"] = True
            row["periodOrig"] = (
                f"{v.get('start_plan_orig') or '—'} → {v.get('end_plan_orig') or '—'}"
            )
        issues_table.append(row)

    behind = sorted(
        [i for i in issues_table if i["lag"] < -1], key=lambda x: x["lag"]
    )[:3]
    ahead = sorted(
        [i for i in issues_table if i["lag"] > 1], key=lambda x: -x["lag"]
    )[:3]

    return build_project_payload(
        proj,
        issue_series,
        proj_start,
        proj_end,
        n_done,
        n_open,
        n_rescheduled,
        w_total,
        categories,
        point_dates,
        views,
        behind,
        ahead,
        options,
        issues_table,
        updated_at,
        build_milestones(issue_series, today),
    )


def build_empty_project(proj: str, today: date, updated_at: str) -> dict:
    d = today.isoformat()
    cat = [today.strftime("%d.%m.%y")]
    empty_view = {
        "key": "ALL",
        "summary": f"Весь проект {proj}",
        "type": "Агрегат",
        "status": "нет данных",
        "start_plan": d,
        "end_plan": d,
        "start_plan_orig": None,
        "end_plan_orig": None,
        "plan_rescheduled": False,
        "end_fact": d,
        "weight": 0,
        "plan_now": 0,
        "fact_now": 0,
        "fact_today": 0,
        "plan_today": 0,
        "sched_today": 0,
        "lag": 0,
        "lag_vs_sched": 0,
        "plan": [0],
        "fact": [None],
        "forecastLinear": [None],
        "forecastPlanLag": [None],
        "forecastCalendar": [None],
    }
    return build_project_payload(
        proj,
        {},
        today,
        today,
        0,
        0,
        0,
        0,
        cat,
        [d],
        {"ALL": empty_view},
        [],
        [],
        [{"value": "ALL", "label": f"Весь проект {proj} (0 этапов)"}],
        [],
        updated_at,
        [],
    )


def build_project_payload(
    proj: str,
    issue_series: dict,
    proj_start: date,
    proj_end: date,
    n_done: int,
    n_open: int,
    n_rescheduled: int,
    w_total: int,
    categories: list[str],
    point_dates: list[str],
    views: dict,
    behind: list[dict],
    ahead: list[dict],
    options: list[dict],
    issues_table: list[dict],
    updated_at: str,
    milestones: list[dict] | None = None,
) -> dict:
    return {
        "id": proj,
        "label": f"{proj} · {PROJECT_LABELS[proj]}",
        "name": PROJECT_LABELS[proj],
        "projStart": proj_start.isoformat(),
        "projEnd": proj_end.isoformat(),
        "nIssues": len(issue_series),
        "nDone": n_done,
        "nOpen": n_open,
        "nRescheduled": n_rescheduled,
        "totalWeight": w_total,
        "categories": categories,
        "pointDates": point_dates,
        "behind": [
            {"key": i["key"], "lag": i["lag"], "summary": i["summary"]} for i in behind
        ],
        "ahead": [
            {"key": i["key"], "lag": i["lag"], "summary": i["summary"]} for i in ahead
        ],
        "options": options,
        "issues": issues_table,
        "views": views,
        "milestones": milestones or [],
        "dataUpdatedAt": updated_at,
    }


def nanify(arr) -> str:
    return "[" + ", ".join("NaN" if v is None else str(v) for v in arr) + "]"


def emit_view(key: str, v: dict) -> str:
    meta_keys = [
        "key",
        "summary",
        "type",
        "status",
        "start_plan",
        "end_plan",
        "start_plan_orig",
        "end_plan_orig",
        "plan_rescheduled",
        "end_fact",
        "weight",
        "plan_now",
        "fact_now",
        "fact_today",
        "plan_today",
        "sched_today",
        "lag",
        "lag_vs_sched",
    ]
    lines = [f'    {json.dumps(key)}: {{']
    for mk in meta_keys:
        val = v.get(mk)
        if mk == "plan_rescheduled" and val is None:
            val = False
        lines.append(f"      {mk}: {json.dumps(val, ensure_ascii=False)},")
    lines.append(f"      plan: {nanify(v['plan'])},")
    lines.append(f"      fact: {nanify(v['fact'])},")
    lines.append(f"      forecastLinear: {nanify(v['forecastLinear'])},")
    lines.append(f"      forecastPlanLag: {nanify(v['forecastPlanLag'])},")
    lines.append(f"      forecastCalendar: {nanify(v['forecastCalendar'])},")
    lines.append("    },")
    return "\n".join(lines)


def emit_project(pid: str, p: dict) -> str:
    lines = [f'  {json.dumps(pid)}: {{']
    scalar = [
        "id",
        "label",
        "name",
        "projStart",
        "projEnd",
        "nIssues",
        "nDone",
        "nOpen",
        "nRescheduled",
        "totalWeight",
    ]
    for k in scalar:
        lines.append(f"    {k}: {json.dumps(p[k], ensure_ascii=False)},")
    lines.append(f"    categories: {json.dumps(p['categories'], ensure_ascii=False)},")
    lines.append(f"    pointDates: {json.dumps(p['pointDates'])},")
    lines.append(f"    behind: {json.dumps(p['behind'], ensure_ascii=False)},")
    lines.append(f"    ahead: {json.dumps(p['ahead'], ensure_ascii=False)},")
    lines.append(f"    options: {json.dumps(p['options'], ensure_ascii=False)},")
    lines.append(f"    issues: {json.dumps(p['issues'], ensure_ascii=False)},")
    lines.append(f"    milestones: {json.dumps(p.get('milestones') or [], ensure_ascii=False)},")
    lines.append("    views: {")
    for k, v in p["views"].items():
        lines.append(emit_view(k, v))
    lines.append("    },")
    if p.get("dataUpdatedAt"):
        lines.append(f"    dataUpdatedAt: {json.dumps(p['dataUpdatedAt'])},")
    lines.append("  },")
    return "\n".join(lines)


def emit_project_options_ts() -> str:
    lines = ["const PROJECT_OPTIONS = ["]
    for pid in ALL_PROJECTS:
        label = f"{pid} · {PROJECT_LABELS[pid]}"
        lines.append(f'  {{ value: {json.dumps(pid)}, label: {json.dumps(label, ensure_ascii=False)} }},')
    lines.append("];")
    return "\n".join(lines)


def emit_project_names_ts() -> str:
    lines = ["const PROJECT_NAMES: Record<string, string> = {"]
    for pid in ALL_PROJECTS:
        lines.append(f'  {pid}: {json.dumps(PROJECT_LABELS[pid], ensure_ascii=False)},')
    lines.append("};")
    return "\n".join(lines)


def emit_project_updated_at_ts(updated_map: dict[str, str], fallback: str) -> str:
    lines = ["const PROJECT_UPDATED_AT: Record<string, string> = {"]
    for pid in ALL_PROJECTS:
        ts = (updated_map.get(pid) or fallback)[:10]
        lines.append(f'  {pid}: "{ts}",')
    lines.append("};")
    return "\n".join(lines)


def patch_canvas_catalog(text: str, snapshot: str) -> str:
    text = re.sub(
        r"/\*\* S-curve · .*? \*/",
        f"/** S-curve · {projects_banner()} · snapshot {snapshot} · task.enco.ru · single project */",
        text,
        count=1,
    )
    text = re.sub(
        r"const PROJECT_OPTIONS = \[[\s\S]*?\n\];",
        emit_project_options_ts(),
        text,
        count=1,
    )
    text = re.sub(
        r"const PROJECT_NAMES: Record<string, string> = \{[\s\S]*?\n\};",
        emit_project_names_ts(),
        text,
        count=1,
    )
    text = re.sub(
        r"Источник: task\.enco\.ru · проекты .*? · данные встроены в canvas",
        f"Источник: task.enco.ru · {projects_banner()} · данные встроены в canvas",
        text,
        count=1,
    )
    text = re.sub(
        r"<Text weight=\"semibold\">Проект</Text> — только один: .*?\.",
        f"<Text weight=\"semibold\">Проект</Text> — один из {len(ALL_PROJECTS)} (общей кривой нет).",
        text,
        count=1,
    )
    return text


def patch_html_catalog(text: str, as_of: str | None = None) -> str:
    banner = projects_banner()
    text = re.sub(
        r"<title>.*?</title>",
        f"<title>S-кривая · {banner}</title>",
        text,
        count=1,
        flags=re.S,
    )
    if as_of:
        text = re.sub(
            r'(<p class="meta">Источник: task\.enco\.ru · снимок ).*?(\s· без общей кривой по проектам</p>)',
            rf"\g<1>{as_of}\2",
            text,
            count=1,
        )
    text = re.sub(
        r"<b>Проект</b> — только один: .*?\.",
        f"<b>Проект</b> — один из {len(ALL_PROJECTS)} (общей кривой нет).",
        text,
        count=1,
    )
    names_block = "const names = " + json.dumps(PROJECT_LABELS, ensure_ascii=False) + ";"
    text = re.sub(r"const names = \{[\s\S]*?\};", names_block, text, count=1)
    ids_js = json.dumps(ALL_PROJECTS)
    text = re.sub(
        r"const PROJECT_IDS = \[[\s\S]*?\];",
        f"const PROJECT_IDS = {ids_js};",
        text,
        count=1,
    )
    return text


def patch_canvas(projects: dict[str, dict], updated_map: dict[str, str]) -> None:
    text = CANVAS.read_text(encoding="utf-8")
    block = "const PROJECTS: Record<string, ProjectData> = {\n" + "\n".join(
        emit_project(pid, projects[pid]) for pid in ALL_PROJECTS if pid in projects
    ) + "\n};"
    text = re.sub(
        r"const PROJECTS: Record<string, ProjectData> = \{[\s\S]*?\n\};",
        block,
        text,
        count=1,
    )
    snapshot = max(updated_map.values())[:10] if updated_map else date.today().isoformat()
    text = re.sub(
        r"const PROJECT_UPDATED_AT: Record<string, string> = \{[\s\S]*?\n\};",
        emit_project_updated_at_ts(updated_map, snapshot),
        text,
        count=1,
    )
    text = patch_canvas_catalog(text, snapshot)
    CANVAS.write_text(text, encoding="utf-8")


def patch_sidecar(projects: dict[str, dict]) -> None:
    data = {}
    if SIDECAR.exists():
        data = read_json(SIDECAR)
    live = data.get("scurve-jira-data", {})
    for pid, p in projects.items():
        live[pid] = json.loads(json.dumps(p))  # NaN -> null via custom below
        # restore arrays with null instead of NaN for JSON
        for view in live[pid]["views"].values():
            for arr_key in (
                "plan",
                "fact",
                "forecastLinear",
                "forecastPlanLag",
                "forecastCalendar",
            ):
                view[arr_key] = [
                    None if v is None else v for v in view[arr_key]
                ]
    data["scurve-jira-data"] = live
    pending = data.setdefault("scurve-refresh-pending", {})
    for pid in projects:
        pending.pop(pid, None)
    if set(projects) >= set(ALL_PROJECTS):
        pending.pop("ALL", None)
    write_json(SIDECAR, data, indent=2)


def project_to_html(p: dict) -> dict:
    """Canvas ProjectData (camelCase) → HTML DATA.projects entry (snake_case)."""
    out = {
        "id": p["id"],
        "label": p["label"],
        "name": p.get("name") or PROJECT_LABELS.get(p["id"], p["label"]),
        "proj_start": p["projStart"],
        "proj_end": p["projEnd"],
        "n_issues": p["nIssues"],
        "n_done": p["nDone"],
        "n_open": p["nOpen"],
        "n_rescheduled": p.get("nRescheduled", 0),
        "total_weight": p["totalWeight"],
        "categories": p["categories"],
        "pointDates": p["pointDates"],
        "views": p["views"],
        "options": p["options"],
        "issues": p["issues"],
        "behind": p["behind"],
        "ahead": p["ahead"],
        "milestones": p.get("milestones") or [],
    }
    # JSON cannot encode NaN; views arrays already use None in sidecar path,
    # but canvas-built dicts may still have None for missing forecast points.
    return json.loads(json.dumps(out))


def render_html(projects: dict[str, dict], as_of: str) -> str:
    text = HTML.read_text(encoding="utf-8")
    m = re.search(r"const DATA = (\{.*?\});\n", text, re.S)
    if not m:
        raise SystemExit("Could not find const DATA in HTML")
    data = json.loads(m.group(1))
    data["asOf"] = as_of
    for pid, p in projects.items():
        data["projects"][pid] = project_to_html(p)
    new_block = "const DATA = " + json.dumps(data, ensure_ascii=False) + ";\n"
    return text[: m.start()] + new_block + text[m.end() :]


def patch_html(projects: dict[str, dict], as_of: str) -> None:
    html = render_html(projects, as_of)
    html = patch_html_catalog(html, as_of)
    HTML.write_text(html, encoding="utf-8")
    # GitHub Pages serves index.html from the repo root.
    (ROOT / "index.html").write_text(html, encoding="utf-8")


def stamp_export_headers(text: str, exported_at: datetime) -> str:
    """Mark export datetime in <title>, <h1>, and .meta."""
    label = exported_at.strftime("%d.%m.%Y %H:%M")
    banner = projects_banner()
    text = re.sub(
        r"<title>.*?</title>",
        f"<title>S-кривая · {banner} · выгрузка {label}</title>",
        text,
        count=1,
        flags=re.S,
    )
    text = re.sub(
        r"<h1>.*?</h1>",
        f"<h1>S-кривая строительных проектов · выгрузка {label}</h1>",
        text,
        count=1,
        flags=re.S,
    )
    text = re.sub(
        r'<p class="meta">.*?</p>',
        (
            f'<p class="meta">Выгрузка: {label} · источник: task.enco.ru '
            f"· {banner} · без общей кривой по проектам</p>"
        ),
        text,
        count=1,
        flags=re.S,
    )
    return text


def export_html_snapshot() -> Path:
    """Write a new timestamped HTML snapshot (does not overwrite s-curve.html)."""
    now = datetime.now().astimezone()
    today = now.date()
    updated_at = now.isoformat(timespec="seconds")
    stamp = now.strftime("%Y-%m-%d_%H%M%S")

    built: dict[str, dict] = {}
    for proj in ALL_PROJECTS:
        cache_dir = CACHE_ROOT / proj
        if project_has_cache(proj):
            print(f"Loading {proj} from local cache...")
            series = load_series_from_cache(proj)
            hier = attach_hierarchies(series, cache_dir, force=False, network=False)
            built[proj] = build_project(proj, series, today, updated_at, hier)
        else:
            print(f"  {proj}: no cache, empty stub")
            built[proj] = build_empty_project(proj, today, updated_at)
            continue
        print(
            f"  {proj}: fact={built[proj]['views']['ALL']['fact_today']} "
            f"plan={built[proj]['views']['ALL']['plan_today']}"
        )

    EXPORTS.mkdir(parents=True, exist_ok=True)
    out = EXPORTS / f"s-curve-{stamp}.html"
    html = stamp_export_headers(render_html(built, as_of=today.isoformat()), now)
    out.write_text(html, encoding="utf-8")

    # Reflect export in canvas sidecar so the UI can show the last path.
    data = {}
    if SIDECAR.exists():
        data = read_json(SIDECAR)
    data["scurve-export-pending"] = ""
    data["scurve-last-export"] = str(out)
    write_json(SIDECAR, data, indent=2)
    return out


def load_embedded_projects() -> dict[str, dict]:
    if SIDECAR.exists():
        data = read_json(SIDECAR)
        live = data.get("scurve-jira-data") or {}
        if live:
            return live
    raise SystemExit(f"No sidecar projects at {SIDECAR}")


def patch_milestones_only() -> None:
    today = date.today()
    built = load_embedded_projects()
    for pid, p in built.items():
        p["milestones"] = milestones_from_project_dict(p, today)
        n = len(p["milestones"])
        print(f"  {pid}: {n} вех")
    updated_map = {
        pid: (p.get("dataUpdatedAt") or today.isoformat())[:10]
        for pid, p in built.items()
    }
    patch_canvas(built, updated_map)
    patch_sidecar(built)
    as_of = max(updated_map.values()) if updated_map else today.isoformat()
    patch_html(built, as_of=as_of)
    print(f"Updated canvas: {CANVAS}")
    print(f"Updated sidecar: {SIDECAR}")
    print(f"Updated HTML: {HTML}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", choices=ALL_PROJECTS + ["ALL"])
    parser.add_argument(
        "--milestones-only",
        action="store_true",
        help="recompute milestone markers from existing sidecar/canvas data (no Jira)",
    )
    parser.add_argument("--force", action="store_true", help="ignore issue cache")
    parser.add_argument(
        "--export-html",
        action="store_true",
        help="write a new timestamped HTML snapshot under exports/",
    )
    args = parser.parse_args()

    if args.milestones_only:
        patch_milestones_only()
        return

    if args.export_html:
        path = export_html_snapshot()
        print(f"Exported HTML: {path}")
        return

    if not args.project:
        parser.error("--project is required unless --export-html or --milestones-only")

    today = date.today()
    updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    targets = ALL_PROJECTS if args.project == "ALL" else [args.project]

    # load existing embedded projects from cache for non-target refresh
    built: dict[str, dict] = {}
    for proj in ALL_PROJECTS:
        cache_dir = CACHE_ROOT / proj
        if proj in targets:
            print(f"Fetching {proj} from Jira...")
            series = load_series(proj, force=args.force)
            hier = attach_hierarchies(
                series, cache_dir, force=args.force, network=True
            )
        elif project_has_cache(proj):
            print(f"Using cache for {proj}...")
            series = load_series(proj, force=False)
            hier = attach_hierarchies(series, cache_dir, force=False, network=False)
            if any(
                series[k]["type"] == "Epic" and not hier.get(k)
                for k in series
            ) and TOKEN:
                hier = attach_hierarchies(
                    series, cache_dir, force=False, network=True
                )
        else:
            print(f"  {proj}: no cache, empty stub")
            built[proj] = build_empty_project(proj, today, updated_at)
            continue
        built[proj] = build_project(proj, series, today, updated_at, hier)
        print(
            f"  {proj}: fact={built[proj]['views']['ALL']['fact_today']} "
            f"plan={built[proj]['views']['ALL']['plan_today']}"
        )

    updated_map = {pid: built[pid]["dataUpdatedAt"][:10] for pid in targets}
    patch_canvas(built, updated_map)
    patch_sidecar({pid: built[pid] for pid in targets})
    # Rebuild all three projects into HTML so shared "today" axis stays consistent.
    patch_html(built, as_of=today.isoformat())
    print(f"Updated canvas: {CANVAS}")
    print(f"Updated sidecar: {SIDECAR}")
    print(f"Updated HTML: {HTML}")


if __name__ == "__main__":
    main()
