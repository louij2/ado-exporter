# ado-exporter

Prometheus exporter for Azure DevOps work items — plus branches, and GitHub — in
`dev.azure.com/vpsgb` and `github.com/louij2`.
Feeds the **Work | Azure DevOps Board** dashboard (`uid: ado-board`).

## Why this exists

The ADO board became the system of record for open work on 2026-08-19, replacing
tracking work in Claude chat threads. Luca deliberately keeps **Homelab** and
**VPS GB** as separate ADO projects — and an ADO board can only ever show its own
project. A third "everything" project would not aggregate them; it would just be a
third empty board.

Two things genuinely span projects in ADO: an org-level WIQL query (a flat list) and
Delivery Plans (a timeline). Neither is a board. So the single cross-project view lives
in Grafana instead, where everything else in the homelab is already watched.

This is a **window onto ADO, not a second tracker**. ADO stays the source of record.

## Metrics

| Metric | Meaning |
|---|---|
| `ado_exporter_last_run_timestamp` | Liveness — unix time of the last successful poll |
| `ado_work_items{project,type,state}` | Counts, including `Done` |
| `ado_open_items_by_epic{project,epic_id,epic}` | Open issues per work stream |
| `ado_open_items_by_tag{tag}` | Open issues per tag |
| `ado_work_item_info{id,project,epic,title,state,tags,assigned_to,url}` | One series per open issue; **value is its age in days** |
| `ado_oldest_open_item_days{project}` | Age of the oldest open issue |
| `ado_exporter_pr_read_ok` | 1 if PRs could be read, 0 if the PAT lacks Code:Read |
| `ado_pull_requests_open{project,repo}` | Open ADO PRs per repo |
| `ado_pull_request_info{id,project,repo,title,author,draft,merge_status,url}` | One series per open ADO PR; **value is its age in days** |
| `ado_branch_read_ok` | 1 if ADO branches could be read |
| `ado_branches_total{project,repo}` | Branches per ADO repo, default branch included |
| `ado_branch_info{project,repo,branch,ahead,behind,merged,url}` | One series per non-default ADO branch; **value is days idle** |
| `github_read_ok` | 1 if GitHub could be read **and the data is trustworthy**, 0 otherwise |
| `github_read_problem{reason}` | Why `read_ok` is 0: `no_token`, `api_error`, `cannot_read_refs` |
| `github_repos_unreadable` | Repos that have been pushed to but report no branches |
| `github_repos{counted}` | Repos seen, split by whether they are counted |
| `github_pull_requests_open{repo}` | Open GitHub PRs per repo |
| `github_pull_request_info{repo,number,title,author,draft,mergeable,head,base,url}` | One series per open GitHub PR; **value is its age in days** |
| `github_branches_total{repo}` | Branches per repo, default branch included |
| `github_branches_compared{outcome}` | Branches compared, and any skipped by `MAX_COMPARES` |
| `github_branches_merged{repo}` / `github_branches_unmerged{repo}` | Split by whether the branch has already landed |
| `github_branch_info{repo,branch,status,ahead,behind,merged,url}` | One series per non-default branch; **value is days idle** |
| `github_rate_limit_remaining` | Requests left in the current GitHub window |
| `code_exporter_last_run_timestamp` | Liveness for the slower branch/GitHub poll |

`ado_work_item_info` carries the title as a label on purpose — that is what makes a
sortable, clickable table panel possible, and it is safe at ~100 items. If the board ever
holds thousands, drop the title label and join on `id` in Grafana instead.

## Branches, and why they are here

The board tracks work items. It never tracked the other place work hides: a branch.
A merged branch nobody deleted and an unmerged branch nobody finished look identical
in any repo listing, and neither is a work item.

`merged` is the label the cleanup decision turns on. `merged="true"` means every commit
on the branch is already in the default branch — it has landed, and deleting it loses
nothing. `merged="false"` means it still holds work the default branch does not have,
which may be abandoned or may be another Claude session's work in progress. **`ahead="-1"`
with `status="unknown"` means the comparison failed** — that branch is deliberately never
counted as merged, because that is the direction that would talk someone into deleting
unmerged work.

GitHub is in this exporter rather than a second one because it answers the same question,
and because reading the two side by side is what catches the mirror trap: `louij2/vpsgb`
has looked authoritative on GitHub since **April 2024** while ADO `ControlPlane/vpsgb` is
the real source of record (ADO work item #89).

Forks and archived repos are skipped — a fork carries the upstream project's entire branch
list (22 on `barrier`, 18 on `kube-thanos`), which is not our work and drowns out what is.
Set `SKIP_FORKS=0` to include them.

Branch and repo listing is far more expensive than the work-item query and changes far
more slowly, so it has its own cache (`CODE_CACHE_SECONDS`, default 600s) and its own
liveness metric. Both halves come back in one `/metrics` response; only the fetch cadence
differs. `MAX_COMPARES` (default 120) caps the per-poll compare calls so a repo that
suddenly sprouts 300 branches cannot turn every scrape into a rate-limit incident —
anything skipped is reported in `github_branches_compared{outcome="skipped"}` so the panel
can never quietly under-report.

## The PAT

Scope it **Work Items: Read** and nothing else. This exporter only ever POSTs a WIQL
query and GETs work items; it never creates, edits, closes or assigns. A read-write PAT
here would grant reach for no reason.

It arrives as a **read-only bind mount**, never an environment variable, because anything
in a container's env is readable via `docker inspect` — which on this box is not
hypothetical (ADO work item #68: the n8n template holds an ADO PAT, a Postgres password
and a webhook secret in plaintext exactly that way).

```bash
install -m 600 -o 9823 /dev/null /mnt/user/appdata/homelab-backup/ado-pat
# paste the token into that file
```

The uid matters: the container runs as 9823, and a wrong owner fails at *read* time, not
start time — the container comes up fine and every scrape returns 502.

Reading PRs needs **Code: Read** as well; that scope is granted separately, so a PAT
holding only Work Items:Read still produces a full set of work-item metrics and reports
`ado_exporter_pr_read_ok 0`. Branch reads use the same Code:Read scope.

## The GitHub token — optional

There is no GitHub token by default and the exporter is fine without one: it reports
`github_read_ok 0` and every ADO metric is untouched. A missing GitHub token must never be
able to take the board dashboard down.

Unauthenticated GitHub is 60 requests an hour, which cannot carry this, so the GitHub half
genuinely needs a token. Create a **fine-grained** PAT on github.com scoped to
**Contents: Read**, **Pull requests: Read**, **Metadata: Read** over `louij2` — read-only,
no write scope anywhere — then mount it the same way as the ADO PAT:

```bash
install -m 600 -o 9823 /dev/null /mnt/user/appdata/homelab-backup/github-token
# paste the token into that file, then re-run deploy.sh
```

`deploy.sh` only passes the mount when the file exists and is non-empty. Bind-mounting a
path that does not exist makes Docker create it as a *directory*, and the exporter would
then be reading a directory and reporting the GitHub half broken for a reason nothing
explains.

Do **not** reuse the classic `GITHUB_PAT` (ADO work item #141) — it is account-wide,
write-capable, and being retired.

### The failure mode with no error in it

**A token that can list repos but not read their contents does not fail.** GraphQL answers
`200`, with no `errors` array, returning each repo's `name`, `isPrivate`, `pushedAt` and
`viewerPermission` — but `defaultBranchRef` null and `refs`/`pullRequests` empty. The
listing looks perfectly healthy and every private repo reads as zero branches and zero
PRs.

Observed live on 2026-08-24 with a classic PAT lacking the `repo` scope: 41 repos listed,
and the only three reporting any branches were the three **public** ones.

Reporting `github_read_ok 1` on that is worse than reporting nothing, because "0 unmerged
branches" reads as good news and actually means blindness. So the exporter checks a
contradiction: **a repo that has ever been pushed to has at least one branch.** `pushedAt`
set, with either no default branch or zero refs, means refs are not readable →
`github_read_ok 0`, `github_read_problem{reason="cannot_read_refs"}`. A genuinely empty
repo has never been pushed (`pushedAt` null) and cannot trigger it.

Do not weaken this to "`defaultBranchRef` is set but `refs` is 0" — that was the first
version and it never fired, because the unreadable case returns null for *both* fields.

If you hit it: a classic PAT needs the whole `repo` scope; a fine-grained PAT needs
Contents:Read **and** Pull requests:Read **and** Metadata:Read, over all repositories.
Metadata alone produces exactly this symptom. `deploy.sh` fails with the reason and the
matching fix rather than completing quietly.

**Fixing the scope and installing the token are two separate steps, and the first one on
its own changes nothing here.** `gh auth refresh -s repo` widens the gh CLI's keychain
token on the Mac; it does not touch `/mnt/user/appdata/homelab-backup/github-token`, so
re-running the deploy just re-reads the same unchanged token and fails identically. This
cost a round trip on 2026-08-24 because the error message named the scope fix without
naming the copy. Install it explicitly:

```bash
pbpaste | ssh root@10.0.0.24 "cat > /mnt/user/appdata/homelab-backup/github-token \
  && chown 9823 /mnt/user/appdata/homelab-backup/github-token \
  && chmod 600 /mnt/user/appdata/homelab-backup/github-token"
```

Prefer a fine-grained read-only PAT over piping `gh auth token`. The gh CLI token is
account-wide and write-capable (`repo`, `workflow`, `gist`) — installing it as a service
credential on tower recreates exactly the problem ADO #141 exists to retire.

## Deploy

```bash
scp -r unraid/ado-exporter root@10.0.0.24:/mnt/user/appdata/
ssh root@10.0.0.24 'bash /mnt/user/appdata/ado-exporter/deploy.sh'
```

`deploy.sh` preflights the token, syntax-checks the exporter, retags the running image as
`:rollback`, rebuilds with `--network=host` (builds on this box have hung on the docker0
bridge), and asserts every expected metric family is present before declaring success.

## Tests

`python3 test_exporter.py` — offline, no network. Covers label escaping (a quote in a
title would otherwise corrupt the exposition format), `Done` exclusion from the open
table while still counting in totals, tag counts not being inflated by closed items,
parentless issues rolling up as `(no epic)`, and age-in-days as the series value.

The GitHub half is covered too: forks excluded, the default branch never reported as a
stale branch, a merged branch counted as merged and an unmerged one as unmerged, an
uncomparable branch reading `unknown` rather than `merged`, and every failure mode
(no token, 401, a GraphQL `errors` array returned with HTTP 200) reporting
`github_read_ok 0` rather than raising.

## Scrape

`prometheus.yml` job `ado`, 5m interval, 1m timeout — the board changes on human
timescales and the ADO API is rate-limited. The exporter caches for 120s independently,
so a scrape storm cannot hammer ADO. The branch/GitHub half caches for 600s on top of
that.

## The dashboard

`scripts/add_github_panels.py` writes the GitHub and branch section into
`grafana/dashboards/work/work-azure-devops-board.json`. It is idempotent — it owns panel
ids 50-58 and replaces them rather than appending a second copy — and it also retitles the
three pre-existing ADO PR panels, because once GitHub PRs are on the same dashboard a
panel called "Open PRs" reads as "all of them" and is in fact ADO only.
