#!/usr/bin/env python3
"""Prometheus exporter for Azure DevOps work items (dev.azure.com/vpsgb).

Exists so the board can be watched from Grafana alongside everything else,
WITHOUT consolidating the ADO projects. Luca deliberately keeps Homelab and
VPS GB as separate ADO projects (2026-08-19); an ADO board can only ever show
its own project, so the single cross-project view lives here instead.

Read-only: only ever POSTs a WIQL *query* and GETs work items. It never
creates, edits, closes or assigns anything. Stdlib only -- nothing to pip
install and no third-party code in the image.

WHY THE TOKEN IS A FILE AND NOT AN ENV VAR
------------------------------------------
Anything in a container's environment is readable by `docker inspect`, and on
this box that is not hypothetical: the n8n template holds an ADO PAT, a
Postgres password and a webhook secret in plaintext exactly that way (ADO work
item #68). So the PAT arrives as a read-only bind mount, same as
semaphore-exporter's token.

The PAT should be scoped **Work Items: Read** and nothing else. This exporter
cannot write, so a read-write PAT here would be granting reach for no reason.

CARDINALITY
-----------
`ado_work_item_info` carries one series per open work item, with the title as a
label. That is deliberate -- it is what makes a sortable table panel possible --
and it is safe at this size: the board holds ~100 items, not 100k. If the board
ever grows past a few thousand open items, drop the title label and join on id
in Grafana instead.
"""
import base64
import http.server
import json
import os
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ORG = os.environ.get("ADO_ORG", "https://dev.azure.com/vpsgb")
TOKEN_FILE = os.environ.get("ADO_PAT_FILE", "/run/secrets/ado-pat")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9823"))
CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "120"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "30"))
API = "api-version=7.1"

# ADO rejects a work-items batch GET above 200 ids.
BATCH = 200

# --- the code half (branches, and GitHub) --------------------------------
#
# Work that lives in a branch is invisible on the board: a merged branch nobody
# deleted and an unmerged branch nobody finished look identical in a repo list,
# and neither shows up as a work item. The metrics below exist so both are
# visible next to the board they belong to.
#
# GitHub is here rather than in a second exporter because it answers the same
# question as the ADO half — "what is open, and what is only pretending to be" —
# and because the mirror trap makes the two halves worth reading side by side:
# louij2/vpsgb has looked authoritative on GitHub since 2024 while ADO
# ControlPlane/vpsgb is the real source of record.
#
# Branch and repo listing is much more expensive than the work-item query and
# changes far more slowly, so it gets its own longer cache. Both halves are
# emitted from one /metrics response; only the fetch cadence differs.
GITHUB_TOKEN_FILE = os.environ.get("GITHUB_TOKEN_FILE", "/run/secrets/github-token")
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "louij2")
GITHUB_API = os.environ.get("GITHUB_API", "https://api.github.com")
CODE_CACHE_SECONDS = int(os.environ.get("CODE_CACHE_SECONDS", "600"))

# Each non-default branch costs one compare call. A hard cap keeps a repo that
# suddenly sprouts 300 branches from turning every scrape into a rate-limit
# incident; anything skipped is reported in github_branches_compared so the
# panel can never quietly under-report.
MAX_COMPARES = int(os.environ.get("MAX_COMPARES", "120"))

# Forks carry the upstream project's entire branch list — 22 on `barrier`, 18 on
# `kube-thanos`. That is not our work and it drowns out what is. Same for
# archived repos, which are read-only by definition.
SKIP_FORKS = os.environ.get("SKIP_FORKS", "1") != "0"

_lock = threading.Lock()
_cache = {"at": 0.0, "body": None}
_code_lock = threading.Lock()
_code_cache = {"at": 0.0, "lines": None}


def _auth():
    with open(TOKEN_FILE, "r", encoding="utf-8") as fh:
        pat = fh.read().strip()
    if not pat:
        raise RuntimeError(f"{TOKEN_FILE} is empty")
    return "Basic " + base64.b64encode(f":{pat}".encode()).decode()


def _api(method, url, body=None, auth=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", auth)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.load(resp)


def _esc(v):
    """Escape a Prometheus label value."""
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _age_days(stamp, now):
    """Days since an ISO-8601 stamp. 0.0 rather than an exception on junk."""
    try:
        return (now - time.mktime(time.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S"))) / 86400.0
    except (ValueError, TypeError):
        return 0.0


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------

def _gh_auth():
    """Bearer header for GitHub, or None if no token is mounted.

    Deliberately not fatal. Unauthenticated GitHub is 60 requests an hour,
    which cannot carry this, so with no token the GitHub half reports itself
    absent (github_read_ok 0) and the ADO half is untouched. A missing token
    must never be able to take the board dashboard down.
    """
    try:
        with open(GITHUB_TOKEN_FILE, "r", encoding="utf-8") as fh:
            tok = fh.read().strip()
    except OSError:
        return None
    return f"Bearer {tok}" if tok else None


def _gh(method, url, body=None, auth=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", auth)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "ado-exporter")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        remaining = resp.headers.get("X-RateLimit-Remaining")
        return json.load(resp), (int(remaining) if remaining is not None else None)


# One GraphQL query returns every repo with its open PRs and its branch list.
# The REST equivalent is two calls per repo — 80+ per poll across 41 repos — and
# that is the difference between a scrape that finishes and one that times out.
GH_QUERY = """
query($login:String!, $cursor:String) {
  repositoryOwner(login:$login) {
    repositories(first:50, after:$cursor, ownerAffiliations:OWNER,
                 orderBy:{field:PUSHED_AT, direction:DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name isFork isArchived pushedAt url
        defaultBranchRef { name }
        pullRequests(states:OPEN, first:50,
                     orderBy:{field:CREATED_AT, direction:DESC}) {
          totalCount
          nodes {
            number title createdAt isDraft mergeable url
            headRefName baseRefName
            author { login }
          }
        }
        refs(refPrefix:"refs/heads/", first:100) {
          totalCount
          nodes { name target { ... on Commit { committedDate } } }
        }
      }
    }
  }
}
"""


def _gh_repos(auth):
    """Every repo the owner owns, with open PRs and branches. Paginated."""
    repos, cursor, remaining = [], None, None
    while True:
        body, rem = _gh("POST", f"{GITHUB_API}/graphql",
                        {"query": GH_QUERY,
                         "variables": {"login": GITHUB_OWNER, "cursor": cursor}}, auth)
        if rem is not None:
            remaining = rem
        # GraphQL answers 200 with an errors array. Treating that as success is
        # how a permissions problem turns into a dashboard full of zeroes.
        if body.get("errors"):
            raise RuntimeError(body["errors"][0].get("message", "graphql error"))
        owner = (body.get("data") or {}).get("repositoryOwner") or {}
        page = owner.get("repositories") or {}
        repos += page.get("nodes") or []
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return repos, remaining
        cursor = info["endCursor"]


def collect_github(now):
    """GitHub PR and branch metrics. Never raises — reports itself instead."""
    out = []
    a = out.append
    auth = _gh_auth()

    a("# HELP github_read_ok 1 if GitHub could be read AND the data is trustworthy, 0 otherwise.")
    a("# TYPE github_read_ok gauge")
    a("# HELP github_read_problem Why github_read_ok is 0. Absent when it is 1.")
    a("# TYPE github_read_problem gauge")

    def unreadable(reason):
        a("github_read_ok 0")
        a(f'github_read_problem{{reason="{reason}"}} 1')
        return out

    if not auth:
        return unreadable("no_token")

    try:
        repos, remaining = _gh_repos(auth)
    except (urllib.error.HTTPError, urllib.error.URLError,
            RuntimeError, KeyError, ValueError):
        return unreadable("api_error")

    live = [r for r in repos
            if not r.get("isArchived") and not (SKIP_FORKS and r.get("isFork"))]

    # A token that cannot read a private repo's contents does NOT fail. GraphQL
    # answers 200, with no errors array, and `defaultBranchRef` null with
    # `refs` and `pullRequests` empty -- while still returning the repo's name,
    # isPrivate, pushedAt and viewerPermission, so the listing looks perfectly
    # healthy. Observed 2026-08-24 with a classic PAT lacking the `repo` scope:
    # 41 repos listed, and the only three reporting any branches were the three
    # PUBLIC ones.
    #
    # Reporting read_ok 1 on that is worse than reporting nothing. "0 unmerged
    # branches" reads as good news and actually means blindness. Worse, the
    # branch loop below skips every ref when the default branch is unknown, so
    # it produces a clean empty result rather than an error.
    #
    # The check is a contradiction, not a heuristic: a repo that has ever been
    # pushed to has at least one branch. So pushedAt set, with either no
    # default branch or zero refs, means refs are not readable. A genuinely
    # empty repo has never been pushed and has pushedAt null, so it cannot
    # trigger this.
    #
    # Do NOT weaken this to "defaultBranchRef is set but refs is 0" -- that was
    # the first version and it never fired, because the unreadable case returns
    # null for BOTH fields.
    blind = [r for r in live
             if r.get("pushedAt")
             and (not (r.get("defaultBranchRef") or {}).get("name")
                  or not (r.get("refs") or {}).get("totalCount"))]
    if blind:
        a("# HELP github_repos_unreadable Repos with a default branch but no visible refs.")
        a("# TYPE github_repos_unreadable gauge")
        a(f"github_repos_unreadable {len(blind)}")
        return unreadable("cannot_read_refs")

    a("github_read_ok 1")

    a("# HELP github_repos Repositories seen, split by whether they are counted.")
    a("# TYPE github_repos gauge")
    a(f'github_repos{{counted="true"}} {len(live)}')
    a(f'github_repos{{counted="false"}} {len(repos) - len(live)}')

    # ---- pull requests ----
    a("# HELP github_pull_requests_open Open pull requests per repository.")
    a("# TYPE github_pull_requests_open gauge")
    total_prs = 0
    for r in live:
        n = (r.get("pullRequests") or {}).get("totalCount", 0)
        total_prs += n
        if n:
            a(f'github_pull_requests_open{{repo="{_esc(r["name"])}"}} {n}')
    if not total_prs:
        # An explicit zero, so the panel reads 0 rather than "No data" when the
        # honest answer is "nothing is open" — same reasoning as the ADO half.
        a('github_pull_requests_open{repo=""} 0')

    a("# HELP github_pull_request_info One series per open PR. Value is its age in days.")
    a("# TYPE github_pull_request_info gauge")
    for r in live:
        for pr in (r.get("pullRequests") or {}).get("nodes") or []:
            a(f'github_pull_request_info{{'
              f'repo="{_esc(r["name"])}",'
              f'number="{pr.get("number","")}",'
              f'title="{_esc(pr.get("title",""))}",'
              f'author="{_esc((pr.get("author") or {}).get("login",""))}",'
              f'draft="{str(bool(pr.get("isDraft"))).lower()}",'
              f'mergeable="{_esc(pr.get("mergeable",""))}",'
              f'head="{_esc(pr.get("headRefName",""))}",'
              f'base="{_esc(pr.get("baseRefName",""))}",'
              f'url="{_esc(pr.get("url",""))}"'
              f'}} {_age_days(pr.get("createdAt"), now):.2f}')

    # ---- branches ----
    #
    # ahead_by is the number the cleanup decision actually turns on. A branch at
    # ahead=0 is fully contained in the default branch: it has already landed and
    # is safe to delete. A branch at ahead>0 still holds work nobody merged, and
    # deleting it loses that work. The two are indistinguishable in any repo
    # listing, which is why they are worth a metric.
    a("# HELP github_branches_total Branches per repository, including the default branch.")
    a("# TYPE github_branches_total gauge")
    a("# HELP github_branches_compared Non-default branches compared, and any skipped by MAX_COMPARES.")
    a("# TYPE github_branches_compared gauge")
    a("# HELP github_branch_info One series per non-default branch. Value is days since its last commit.")
    a("# TYPE github_branch_info gauge")
    a("# HELP github_branches_merged Non-default branches fully contained in the default branch — safe to delete.")
    a("# TYPE github_branches_merged gauge")
    a("# HELP github_branches_unmerged Non-default branches holding commits the default branch does not have.")
    a("# TYPE github_branches_unmerged gauge")

    counts, merged, unmerged, infos = [], {}, {}, []
    budget = MAX_COMPARES
    skipped = 0
    for r in live:
        name = r["name"]
        refs = r.get("refs") or {}
        counts.append((name, refs.get("totalCount", 0)))
        dflt = (r.get("defaultBranchRef") or {}).get("name")
        for ref in refs.get("nodes") or []:
            bn = ref.get("name")
            if not bn or bn == dflt or not dflt:
                continue
            if budget <= 0:
                skipped += 1
                continue
            budget -= 1
            try:
                cmp_, rem = _gh("GET", f"{GITHUB_API}/repos/{GITHUB_OWNER}/"
                                       f"{urllib.parse.quote(name)}/compare/"
                                       f"{urllib.parse.quote(dflt)}...{urllib.parse.quote(bn)}",
                                None, auth)
                if rem is not None:
                    remaining = rem
                ahead, behind = cmp_.get("ahead_by", 0), cmp_.get("behind_by", 0)
                status = cmp_.get("status", "")
            except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
                # A branch we could not compare is reported as unknown rather
                # than silently counted as merged — the direction that loses work.
                ahead, behind, status = -1, -1, "unknown"
            if ahead == 0:
                merged[name] = merged.get(name, 0) + 1
            elif ahead > 0:
                unmerged[name] = unmerged.get(name, 0) + 1
            age = _age_days((ref.get("target") or {}).get("committedDate"), now)
            infos.append(
                f'github_branch_info{{'
                f'repo="{_esc(name)}",'
                f'branch="{_esc(bn)}",'
                f'status="{_esc(status)}",'
                f'ahead="{ahead}",'
                f'behind="{behind}",'
                f'merged="{str(ahead == 0).lower()}",'
                f'url="{_esc(r.get("url",""))}/tree/{urllib.parse.quote(bn)}"'
                f'}} {age:.2f}')

    for name, n in counts:
        a(f'github_branches_total{{repo="{_esc(name)}"}} {n}')
    a(f"github_branches_compared{{outcome=\"compared\"}} {MAX_COMPARES - budget}")
    a(f'github_branches_compared{{outcome="skipped"}} {skipped}')
    for name, n in sorted(merged.items()):
        a(f'github_branches_merged{{repo="{_esc(name)}"}} {n}')
    if not merged:
        a('github_branches_merged{repo=""} 0')
    for name, n in sorted(unmerged.items()):
        a(f'github_branches_unmerged{{repo="{_esc(name)}"}} {n}')
    if not unmerged:
        a('github_branches_unmerged{repo=""} 0')
    out += infos

    if remaining is not None:
        a("# HELP github_rate_limit_remaining Requests left in the current GitHub rate-limit window.")
        a("# TYPE github_rate_limit_remaining gauge")
        a(f"github_rate_limit_remaining {remaining}")
    return out


# --------------------------------------------------------------------------
# ADO branches
# --------------------------------------------------------------------------

def collect_ado_branches(now, auth):
    """Same branch question, asked of ADO. Never raises."""
    out = []
    a = out.append
    a("# HELP ado_branch_read_ok 1 if ADO branches could be read, 0 if the PAT lacks Code:Read.")
    a("# TYPE ado_branch_read_ok gauge")
    try:
        projects = [p["name"] for p in
                    _api("GET", f"{ORG}/_apis/projects?{API}", None, auth)["value"]]
    except (urllib.error.HTTPError, urllib.error.URLError, KeyError, ValueError):
        a("ado_branch_read_ok 0")
        return out

    rows, totals = [], []
    ok = 0
    for proj in projects:
        try:
            repos = _api("GET", f"{ORG}/{urllib.parse.quote(proj)}/_apis/git/"
                                f"repositories?{API}", None, auth)["value"]
        except (urllib.error.HTTPError, urllib.error.URLError, KeyError, ValueError):
            continue
        ok = 1
        for rp in repos:
            rid, rname = rp["id"], rp["name"]
            dflt = (rp.get("defaultBranch") or "").replace("refs/heads/", "")
            base = f"{ORG}/{urllib.parse.quote(proj)}/_apis/git/repositories/{rid}"
            try:
                refs = _api("GET", f"{base}/refs?filter=heads&{API}", None, auth)["value"]
            except (urllib.error.HTTPError, urllib.error.URLError, KeyError, ValueError):
                continue
            totals.append((proj, rname, len(refs)))
            for ref in refs:
                bn = ref["name"].replace("refs/heads/", "")
                if not dflt or bn == dflt:
                    continue
                try:
                    st = _api("GET", f"{base}/stats/branches?"
                                     f"name={urllib.parse.quote(bn)}&{API}", None, auth)
                    ahead, behind = st.get("aheadCount", -1), st.get("behindCount", -1)
                    age = _age_days(((st.get("commit") or {}).get("committer") or {}).get("date"), now)
                except (urllib.error.HTTPError, urllib.error.URLError, KeyError, ValueError):
                    ahead, behind, age = -1, -1, 0.0
                rows.append(
                    f'ado_branch_info{{'
                    f'project="{_esc(proj)}",'
                    f'repo="{_esc(rname)}",'
                    f'branch="{_esc(bn)}",'
                    f'ahead="{ahead}",'
                    f'behind="{behind}",'
                    f'merged="{str(ahead == 0).lower()}",'
                    f'url="{_esc(ORG)}/{urllib.parse.quote(proj)}/_git/'
                    f'{urllib.parse.quote(rname)}?version=GB{urllib.parse.quote(bn)}"'
                    f'}} {age:.2f}')
    a(f"ado_branch_read_ok {ok}")
    a("# HELP ado_branches_total Branches per ADO repository, including the default branch.")
    a("# TYPE ado_branches_total gauge")
    for proj, rname, n in totals:
        a(f'ado_branches_total{{project="{_esc(proj)}",repo="{_esc(rname)}"}} {n}')
    a("# HELP ado_branch_info One series per non-default ADO branch. Value is days since its last commit.")
    a("# TYPE ado_branch_info gauge")
    out += rows
    return out


def collect_code():
    """The slow half: ADO branches plus everything GitHub."""
    now = time.time()
    lines = []
    try:
        lines += collect_ado_branches(now, _auth())
    except Exception:                       # noqa: BLE001 - never break the ADO half
        pass
    lines += collect_github(now)
    lines.append("# HELP code_exporter_last_run_timestamp Unix time of the last branch/GitHub poll.")
    lines.append("# TYPE code_exporter_last_run_timestamp gauge")
    lines.append(f"code_exporter_last_run_timestamp {now:.0f}")
    return lines


def code_cached():
    with _code_lock:
        if _code_cache["lines"] is not None and \
                (time.time() - _code_cache["at"]) < CODE_CACHE_SECONDS:
            return _code_cache["lines"]
        lines = collect_code()
        _code_cache.update(at=time.time(), lines=lines)
        return lines


def collect():
    auth = _auth()

    # One org-level WIQL with no @project macro -- this is the whole point of
    # the exporter, and it is the only ADO query that spans projects.
    wiql = ("SELECT [System.Id] FROM WorkItems "
            "WHERE [System.WorkItemType] IN ('Issue','Epic') "
            "ORDER BY [System.Id]")
    res = _api("POST", f"{ORG}/_apis/wit/wiql?{API}", {"query": wiql}, auth)
    ids = [str(w["id"]) for w in res.get("workItems", [])]

    fields = ",".join([
        "System.Id", "System.Title", "System.State", "System.WorkItemType",
        "System.Tags", "System.TeamProject", "System.Parent",
        "System.CreatedDate", "System.AssignedTo",
    ])
    items = []
    for i in range(0, len(ids), BATCH):
        chunk = ",".join(ids[i:i + BATCH])
        items += _api("GET", f"{ORG}/_apis/wit/workitems?ids={chunk}&fields={fields}&{API}",
                      None, auth)["value"]

    epics = {w["id"]: w["fields"].get("System.Title", "")
             for w in items if w["fields"].get("System.WorkItemType") == "Epic"}

    now = time.time()
    out = []
    a = out.append

    a("# HELP ado_exporter_last_run_timestamp Unix time of the last successful ADO poll.")
    a("# TYPE ado_exporter_last_run_timestamp gauge")
    a(f"ado_exporter_last_run_timestamp {now:.0f}")

    a("# HELP ado_work_items Work item count by project, type and state.")
    a("# TYPE ado_work_items gauge")
    counts = {}
    for w in items:
        f = w["fields"]
        k = (f.get("System.TeamProject", ""), f.get("System.WorkItemType", ""),
             f.get("System.State", ""))
        counts[k] = counts.get(k, 0) + 1
    for (proj, typ, state), n in sorted(counts.items()):
        a(f'ado_work_items{{project="{_esc(proj)}",type="{_esc(typ)}",state="{_esc(state)}"}} {n}')

    issues = [w for w in items if w["fields"].get("System.WorkItemType") == "Issue"]
    openish = [w for w in issues if w["fields"].get("System.State") != "Done"]

    a("# HELP ado_open_items_by_epic Open (not Done) issues per epic.")
    a("# TYPE ado_open_items_by_epic gauge")
    by_epic = {}
    for w in openish:
        f = w["fields"]
        pid = f.get("System.Parent")
        key = (f.get("System.TeamProject", ""), str(pid or ""), epics.get(pid, "(no epic)"))
        by_epic[key] = by_epic.get(key, 0) + 1
    for (proj, eid, ename), n in sorted(by_epic.items()):
        a(f'ado_open_items_by_epic{{project="{_esc(proj)}",epic_id="{_esc(eid)}",'
          f'epic="{_esc(ename)}"}} {n}')

    a("# HELP ado_open_items_by_tag Open issues carrying each tag.")
    a("# TYPE ado_open_items_by_tag gauge")
    by_tag = {}
    for w in openish:
        for t in [t.strip() for t in (w["fields"].get("System.Tags") or "").split(";") if t.strip()]:
            by_tag[t] = by_tag.get(t, 0) + 1
    for t, n in sorted(by_tag.items()):
        a(f'ado_open_items_by_tag{{tag="{_esc(t)}"}} {n}')

    a("# HELP ado_work_item_info One series per open issue. Value is its age in days.")
    a("# TYPE ado_work_item_info gauge")
    for w in openish:
        f = w["fields"]
        pid = f.get("System.Parent")
        age = _age_days(f.get("System.CreatedDate", ""), now)
        assigned = (f.get("System.AssignedTo") or {}).get("displayName", "unassigned")
        proj = f.get("System.TeamProject", "")
        a(f'ado_work_item_info{{'
          f'id="{w["id"]}",'
          f'project="{_esc(proj)}",'
          f'epic="{_esc(epics.get(pid, "(no epic)"))}",'
          f'title="{_esc(f.get("System.Title", ""))}",'
          f'state="{_esc(f.get("System.State", ""))}",'
          f'tags="{_esc(f.get("System.Tags") or "")}",'
          f'assigned_to="{_esc(assigned)}",'
          f'url="{_esc(ORG)}/{urllib.parse.quote(proj)}/_workitems/edit/{w["id"]}"'
          f'}} {age:.2f}')

    # ---- pull requests -----------------------------------------------
    #
    # Deliberately in its own try/except. Reading PRs needs Code:Read while
    # work items need Work Items:Read, and the two scopes are granted
    # separately -- so a PAT that legitimately cannot see PRs must still
    # produce a full set of work-item metrics rather than a 502 for the
    # whole scrape.
    pr_ok, prs = 1, []
    try:
        prs = _api("GET",
                   f"{ORG}/_apis/git/pullrequests?searchCriteria.status=active"
                   f"&$top=200&{API}", None, auth).get("value", [])
    except (urllib.error.HTTPError, urllib.error.URLError, KeyError):
        pr_ok = 0

    a("# HELP ado_exporter_pr_read_ok 1 if pull requests could be read, 0 if the PAT lacks Code:Read.")
    a("# TYPE ado_exporter_pr_read_ok gauge")
    a(f"ado_exporter_pr_read_ok {pr_ok}")

    a("# HELP ado_pull_requests_open Open pull requests by project and repo.")
    a("# TYPE ado_pull_requests_open gauge")
    pr_counts = {}
    for pr in prs:
        repo = pr.get("repository") or {}
        key = ((repo.get("project") or {}).get("name", ""), repo.get("name", ""))
        pr_counts[key] = pr_counts.get(key, 0) + 1
    for (proj, repo), n in sorted(pr_counts.items()):
        a(f'ado_pull_requests_open{{project="{_esc(proj)}",repo="{_esc(repo)}"}} {n}')
    if not pr_counts and pr_ok:
        # An explicit zero, so the panel reads 0 rather than "No data" when the
        # honest answer is "nothing is open".
        a('ado_pull_requests_open{project="",repo=""} 0')

    a("# HELP ado_pull_request_info One series per open PR. Value is its age in days.")
    a("# TYPE ado_pull_request_info gauge")
    for pr in prs:
        repo = pr.get("repository") or {}
        proj = (repo.get("project") or {}).get("name", "")
        age = _age_days(pr.get("creationDate", ""), now)
        # mergeStatus "conflicts" is the one worth seeing at a glance: the PR
        # is open, looks fine in a list, and cannot be completed.
        a(f'ado_pull_request_info{{'
          f'id="{pr.get("pullRequestId","")}",'
          f'project="{_esc(proj)}",'
          f'repo="{_esc(repo.get("name",""))}",'
          f'title="{_esc(pr.get("title",""))}",'
          f'author="{_esc((pr.get("createdBy") or {}).get("displayName",""))}",'
          f'draft="{str(bool(pr.get("isDraft"))).lower()}",'
          f'merge_status="{_esc(pr.get("mergeStatus",""))}",'
          f'url="{_esc(ORG)}/{urllib.parse.quote(proj)}/_git/'
          f'{urllib.parse.quote(repo.get("name",""))}/pullrequest/{pr.get("pullRequestId","")}"'
          f'}} {age:.2f}')

    a("# HELP ado_oldest_open_item_days Age of the oldest open issue, per project.")
    a("# TYPE ado_oldest_open_item_days gauge")
    oldest = {}
    for w in openish:
        f = w["fields"]
        try:
            age = (now - time.mktime(time.strptime(
                f.get("System.CreatedDate", "")[:19], "%Y-%m-%dT%H:%M:%S"))) / 86400.0
        except (ValueError, TypeError):
            continue
        p = f.get("System.TeamProject", "")
        oldest[p] = max(oldest.get(p, 0.0), age)
    for p, d in sorted(oldest.items()):
        a(f'ado_oldest_open_item_days{{project="{_esc(p)}"}} {d:.2f}')

    # Branches and GitHub, on their own slower cache. Appended here so a scrape
    # is still one response; only the fetch cadence differs.
    out += code_cached()

    return "\n".join(out) + "\n"


def cached():
    with _lock:
        if _cache["body"] and (time.time() - _cache["at"]) < CACHE_SECONDS:
            return _cache["body"]
        body = collect()
        _cache.update(at=time.time(), body=body)
        return body


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] not in ("/metrics", "/"):
            self.send_error(404)
            return
        try:
            body = cached().encode()
        except Exception as exc:            # noqa: BLE001 - surfaced in the body on purpose
            # 502 with the reason in the body. `docker logs` only records the
            # status line, which is how semaphore-exporter's token permission
            # failure went unexplained for a deploy.
            msg = f"{type(exc).__name__}: {exc}".encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    if "--once" in sys.argv:                 # for deploy-time verification
        sys.stdout.write(collect())
        sys.exit(0)
    with Server(("", LISTEN_PORT), Handler) as httpd:
        sys.stderr.write(f"ado-exporter listening on :{LISTEN_PORT}\n")
        httpd.serve_forever()
