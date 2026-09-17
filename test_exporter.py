"""Offline tests for ado-exporter: label escaping, epic mapping, tag counting,
and the GitHub half.

Run:  python3 test_exporter.py
No network — the ADO calls are stubbed, so this is safe to run anywhere.
"""
import json, os, sys, time, types, unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("ADO_PAT_FILE", "/dev/null")
import exporter  # noqa: E402

NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400 * 3))

ITEMS = [
    {"id": 1, "fields": {"System.Id": 1, "System.Title": "Networking",
                         "System.WorkItemType": "Epic", "System.State": "To Do",
                         "System.TeamProject": "Homelab", "System.CreatedDate": NOW}},
    {"id": 2, "fields": {"System.Id": 2, "System.Title": 'Quote " and \\ backslash',
                         "System.WorkItemType": "Issue", "System.State": "To Do",
                         "System.TeamProject": "Homelab", "System.Parent": 1,
                         "System.Tags": "security; hands-on", "System.CreatedDate": NOW,
                         "System.AssignedTo": {"displayName": "LUCA CHANA"}}},
    {"id": 3, "fields": {"System.Id": 3, "System.Title": "Done thing",
                         "System.WorkItemType": "Issue", "System.State": "Done",
                         "System.TeamProject": "ControlPlane", "System.Parent": 1,
                         "System.Tags": "security", "System.CreatedDate": NOW}},
    {"id": 4, "fields": {"System.Id": 4, "System.Title": "Orphan",
                         "System.WorkItemType": "Issue", "System.State": "To Do",
                         "System.TeamProject": "ControlPlane", "System.CreatedDate": NOW}},
]


PRS = [
    {"pullRequestId": 46, "title": 'PR with a " quote', "isDraft": False,
     "mergeStatus": "succeeded", "creationDate": NOW,
     "createdBy": {"displayName": "LUCA CHANA"},
     "repository": {"name": "vpsgb", "project": {"name": "ControlPlane"}}},
    {"pullRequestId": 47, "title": "Conflicted one", "isDraft": True,
     "mergeStatus": "conflicts", "creationDate": NOW,
     "createdBy": {"displayName": "LUCA CHANA"},
     "repository": {"name": "vpsgb", "project": {"name": "ControlPlane"}}},
]

# Set to an exception class to simulate a PAT without Code:Read.
PR_FAILS = [None]


def fake_api(method, url, body=None, auth=None):
    if "pullrequests" in url:
        if PR_FAILS[0]:
            raise PR_FAILS[0]
        return {"value": PRS}
    if "wiql" in url:
        return {"workItems": [{"id": i["id"]} for i in ITEMS]}
    return {"value": ITEMS}


def run():
    # code_cached() is the slow half (ADO branches + GitHub) and it is stubbed
    # out here so these stay pure work-item tests with no network at all. The
    # GitHub half gets its own stubs further down.
    with mock.patch.object(exporter, "_auth", lambda: "Basic x"), \
         mock.patch.object(exporter, "_api", fake_api), \
         mock.patch.object(exporter, "code_cached", lambda: []):
        return exporter.collect()


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"   {detail}"))
    return cond


out = run()
lines = out.splitlines()
ok = True

# A quote or backslash in a title must not break the exposition format.
ok &= check("title quotes/backslashes are escaped",
            'title="Quote \\" and \\\\ backslash"' in out,
            [l for l in lines if 'id="2"' in l][:1])

# Done items must not appear in the open-item table.
ok &= check("Done issues are excluded from ado_work_item_info",
            not any('id="3"' in l for l in lines if l.startswith("ado_work_item_info")))

# ...but they must still be counted in ado_work_items.
ok &= check("Done issues still counted in ado_work_items",
            'ado_work_items{project="ControlPlane",type="Issue",state="Done"} 1' in out)

# A tag on a Done item must not inflate the open-tag count. Both items carry
# `security`, but only one is open.
ok &= check("tag counts only include open issues",
            'ado_open_items_by_tag{tag="security"} 1' in out,
            [l for l in lines if "by_tag" in l])

# An issue with no parent must not vanish from the epic rollup.
ok &= check("parentless issues roll up as (no epic)",
            any('epic="(no epic)"' in l for l in lines if l.startswith("ado_open_items_by_epic")))

# The epic name must be resolved from the parent id, not left numeric.
ok &= check("epic id resolves to its title",
            any('epic="Networking"' in l for l in lines if l.startswith("ado_work_item_info")))

# Age is the metric value; a 3-day-old item should read ~3.
age = [l for l in lines if l.startswith("ado_work_item_info") and 'id="2"' in l]
ok &= check("age in days is the series value",
            bool(age) and 2.9 < float(age[0].rsplit(" ", 1)[1]) < 3.1,
            age)

# Every non-comment line must be parseable as `name{labels} value`.
bad = [l for l in lines if l and not l.startswith("#") and not l.rsplit(" ", 1)[-1]
       .replace(".", "").replace("-", "").isdigit()]
ok &= check("every series line ends in a numeric value", not bad, bad[:2])

# ---- pull requests --------------------------------------------------------

ok &= check("PR read reports success", "ado_exporter_pr_read_ok 1" in out)
ok &= check("open PRs counted per project and repo",
            'ado_pull_requests_open{project="ControlPlane",repo="vpsgb"} 2' in out)
ok &= check("a PR title with a quote is escaped",
            'title="PR with a \\" quote"' in out,
            [l for l in lines if 'id="46"' in l][:1])
ok &= check("the PR deep link is built from project and repo",
            'https://dev.azure.com/vpsgb/ControlPlane/_git/vpsgb/pullrequest/46' in out)
ok &= check("mergeStatus is exposed so conflicts are visible",
            'merge_status="conflicts"' in out)
ok &= check("draft state is exposed", 'draft="true"' in out and 'draft="false"' in out)

# The isolation that matters: PRs need Code:Read, work items need Work
# Items:Read. A PAT holding only the latter must still produce a full set of
# work-item metrics rather than failing the whole scrape.
import urllib.error  # noqa: E402
PR_FAILS[0] = urllib.error.HTTPError("u", 403, "Forbidden", None, None)
degraded = run()
PR_FAILS[0] = None

ok &= check("a PR 403 does not break the scrape",
            "ado_work_item_info" in degraded and "ado_work_items{" in degraded)
ok &= check("  ...and is reported as pr_read_ok 0",
            "ado_exporter_pr_read_ok 0" in degraded)
ok &= check("  ...with no PR series emitted",
            "ado_pull_request_info{" not in degraded)

# ---- GitHub ---------------------------------------------------------------

GH_REPOS = [
    # A live repo: one open PR, one merged branch, one unmerged branch.
    {"name": "grafana-homelab", "isFork": False, "isArchived": False,
     "pushedAt": NOW, "url": "https://github.com/louij2/grafana-homelab",
     "defaultBranchRef": {"name": "main"},
     "pullRequests": {"totalCount": 1, "nodes": [
         {"number": 9, "title": 'PR with a " quote', "createdAt": NOW,
          "isDraft": False, "mergeable": "MERGEABLE",
          "url": "https://github.com/louij2/grafana-homelab/pull/9",
          "headRefName": "feat/x", "baseRefName": "main",
          "author": {"login": "louij2"}}]},
     "refs": {"totalCount": 3, "nodes": [
         {"name": "main", "target": {"committedDate": NOW}},
         {"name": "fix/already-landed", "target": {"committedDate": NOW}},
         {"name": "feat/x", "target": {"committedDate": NOW}}]}},
    # A fork. Its 22 upstream branches are not our work and must not be counted.
    {"name": "barrier", "isFork": True, "isArchived": False,
     "pushedAt": NOW, "url": "https://github.com/louij2/barrier",
     "defaultBranchRef": {"name": "master"},
     "pullRequests": {"totalCount": 0, "nodes": []},
     "refs": {"totalCount": 22, "nodes": [
         {"name": "master", "target": {"committedDate": NOW}},
         {"name": "upstream/noise", "target": {"committedDate": NOW}}]}},
]

# ahead_by is what the delete decision turns on: 0 means already in main.
GH_COMPARE = {"fix/already-landed": {"status": "behind", "ahead_by": 0, "behind_by": 9},
              "feat/x": {"status": "diverged", "ahead_by": 1, "behind_by": 2}}

GH_FAILS = [None]


def fake_gh(method, url, body=None, auth=None):
    if GH_FAILS[0]:
        raise GH_FAILS[0]
    if url.endswith("/graphql"):
        return ({"data": {"repositoryOwner": {"repositories": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": GH_REPOS}}}}, 4321)
    branch = url.rsplit("...", 1)[-1]
    return (GH_COMPARE[branch], 4321)


def run_github():
    with mock.patch.object(exporter, "_gh_auth", lambda: "Bearer x"), \
         mock.patch.object(exporter, "_gh", fake_gh):
        return "\n".join(exporter.collect_github(time.time()))


gh = run_github()
gh_lines = gh.splitlines()

ok &= check("GitHub read reports success", "github_read_ok 1" in gh)
ok &= check("forks are excluded from the counted set",
            'github_repos{counted="true"} 1' in gh and 'github_repos{counted="false"} 1' in gh,
            [l for l in gh_lines if l.startswith("github_repos")])
ok &= check("a fork's branches are not compared",
            not any('repo="barrier"' in l for l in gh_lines))
ok &= check("open PRs counted per repo",
            'github_pull_requests_open{repo="grafana-homelab"} 1' in gh)
ok &= check("a GitHub PR title with a quote is escaped",
            'title="PR with a \\" quote"' in gh,
            [l for l in gh_lines if l.startswith("github_pull_request_info")][:1])
ok &= check("the PR url comes straight from the API",
            "https://github.com/louij2/grafana-homelab/pull/9" in gh)

# The distinction the whole branch section exists for. ahead=0 is already in
# main and safe to delete; ahead>0 still holds work that deleting would lose.
ok &= check("a fully-merged branch is counted as merged",
            'github_branches_merged{repo="grafana-homelab"} 1' in gh,
            [l for l in gh_lines if "branches_merged" in l])
ok &= check("an unmerged branch is counted as unmerged",
            'github_branches_unmerged{repo="grafana-homelab"} 1' in gh,
            [l for l in gh_lines if "branches_unmerged" in l])
ok &= check("merged=true/false is on the branch series",
            'branch="fix/already-landed"' in gh and 'merged="true"' in gh
            and 'merged="false"' in gh)
ok &= check("the default branch is not reported as a stale branch",
            not any('branch="main"' in l for l in gh_lines))
ok &= check("rate limit headroom is exposed",
            "github_rate_limit_remaining 4321" in gh)
ok &= check("every GitHub series line ends in a numeric value",
            not [l for l in gh_lines if l and not l.startswith("#")
                 and not l.rsplit(" ", 1)[-1].replace(".", "").replace("-", "").isdigit()])

# A branch that cannot be compared must read `unknown`, never `merged` — the
# direction that would talk someone into deleting unmerged work.
def fake_gh_bad_compare(method, url, body=None, auth=None):
    if url.endswith("/graphql"):
        return fake_gh(method, url, body, auth)
    raise urllib.error.HTTPError(url, 500, "boom", None, None)


with mock.patch.object(exporter, "_gh_auth", lambda: "Bearer x"), \
     mock.patch.object(exporter, "_gh", fake_gh_bad_compare):
    unknown = "\n".join(exporter.collect_github(time.time()))
ok &= check("an uncomparable branch reads unknown, not merged",
            'status="unknown"' in unknown and 'ahead="-1"' in unknown
            and 'github_branches_merged{repo=""} 0' in unknown,
            [l for l in unknown.splitlines() if "branch_info" in l][:1])

# No token mounted is the normal state until Luca creates one. It must report
# itself, not take the scrape down.
with mock.patch.object(exporter, "_gh_auth", lambda: None):
    notoken = "\n".join(exporter.collect_github(time.time()))
ok &= check("no GitHub token reports read_ok 0",
            "github_read_ok 0" in notoken and "github_pull_request_info{" not in notoken)
ok &= check("  ...with the reason named",
            'github_read_problem{reason="no_token"} 1' in notoken)

GH_FAILS[0] = urllib.error.HTTPError("u", 401, "Bad credentials", None, None)
with mock.patch.object(exporter, "_gh_auth", lambda: "Bearer x"), \
     mock.patch.object(exporter, "_gh", fake_gh):
    bad = "\n".join(exporter.collect_github(time.time()))
GH_FAILS[0] = None
ok &= check("a GitHub 401 reports read_ok 0 rather than raising",
            "github_read_ok 0" in bad and 'reason="api_error"' in bad)

# GraphQL answers 200 with an errors array; treating that as success is how a
# permissions problem becomes a dashboard full of zeroes.
with mock.patch.object(exporter, "_gh_auth", lambda: "Bearer x"), \
     mock.patch.object(exporter, "_gh",
                       lambda *a, **k: ({"errors": [{"message": "no"}]}, 1)):
    gqlerr = "\n".join(exporter.collect_github(time.time()))
ok &= check("a GraphQL errors array is treated as failure",
            "github_read_ok 0" in gqlerr and 'reason="api_error"' in gqlerr)

# The failure that has no error in it at all. A token that cannot read a private
# repo's contents gets 200, no errors array, and every refs/pullRequests field
# silently empty -- while name/isPrivate/viewerPermission still come back, so the
# listing looks healthy. Observed live 2026-08-24 with a classic PAT missing the
# `repo` scope: 41 repos listed, only the 3 PUBLIC ones reporting any branches.
# Reporting read_ok 1 on that is worse than reporting nothing, because
# "0 unmerged branches" reads as good news and actually means blindness.
# This is the exact shape the live API returned on 2026-08-24: pushedAt today,
# and defaultBranchRef null with refs empty. Note BOTH are null -- an earlier
# version of the check keyed on defaultBranchRef being set, and so never fired.
BLIND = [{"name": "grafana-homelab", "isFork": False, "isArchived": False,
          "pushedAt": NOW,                               # <- has been pushed to
          "url": "https://github.com/louij2/grafana-homelab",
          "defaultBranchRef": None,                      # <- but no default branch
          "pullRequests": {"totalCount": 0, "nodes": []},
          "refs": {"totalCount": 0, "nodes": []}}]       # <- and zero refs: impossible


def fake_gh_blind(method, url, body=None, auth=None):
    return ({"data": {"repositoryOwner": {"repositories": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": BLIND}}}}, 4321)


with mock.patch.object(exporter, "_gh_auth", lambda: "Bearer x"), \
     mock.patch.object(exporter, "_gh", fake_gh_blind):
    scopeless = "\n".join(exporter.collect_github(time.time()))
ok &= check("a repo with a default branch but no refs is caught as unreadable",
            "github_read_ok 0" in scopeless
            and 'github_read_problem{reason="cannot_read_refs"} 1' in scopeless
            and "github_repos_unreadable 1" in scopeless,
            [l for l in scopeless.splitlines() if not l.startswith("#")])
ok &= check("  ...and emits no branch or PR series to be misread",
            "github_branch_info{" not in scopeless
            and "github_branches_unmerged{" not in scopeless)

HALF_BLIND = [dict(BLIND[0], defaultBranchRef={"name": "main"})]


def fake_gh_half(method, url, body=None, auth=None):
    return ({"data": {"repositoryOwner": {"repositories": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": HALF_BLIND}}}}, 4321)


with mock.patch.object(exporter, "_gh_auth", lambda: "Bearer x"), \
     mock.patch.object(exporter, "_gh", fake_gh_half):
    half = "\n".join(exporter.collect_github(time.time()))
ok &= check("a pushed repo with a default branch but zero refs is also caught",
            "github_read_ok 0" in half
            and 'github_read_problem{reason="cannot_read_refs"} 1' in half)

# A genuinely empty repo has never been pushed to, so it must NOT trip the check.
EMPTY = [dict(BLIND[0], name="fresh-repo", pushedAt=None)]


def fake_gh_empty(method, url, body=None, auth=None):
    return ({"data": {"repositoryOwner": {"repositories": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": EMPTY}}}}, 4321)


with mock.patch.object(exporter, "_gh_auth", lambda: "Bearer x"), \
     mock.patch.object(exporter, "_gh", fake_gh_empty):
    empty = "\n".join(exporter.collect_github(time.time()))
ok &= check("a never-pushed empty repo is not a false positive",
            "github_read_ok 1" in empty
            and not [l for l in empty.splitlines()
                     if l.startswith("github_read_problem{")],
            [l for l in empty.splitlines() if not l.startswith("#")])

print("\nOK" if ok else "\nFAILURES")
sys.exit(0 if ok else 1)
