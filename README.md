# ado-exporter

Prometheus exporter for Azure DevOps work items, pull requests and
branches — plus, optionally, the same for a GitHub repo. Useful if you want
an ADO board watched from Grafana alongside everything else, or want a
single cross-project view that an ADO board itself can't give you (a board
can only ever show its own project).

## Why branches are here

The board tracks work items. It never tracks the other place work hides: a
branch. A merged branch nobody deleted and an unmerged branch nobody
finished look identical in any repo listing, and neither is a work item.

`merged` is the label the cleanup decision turns on. `merged="true"` means
every commit on the branch is already in the default branch — it has
landed, and deleting it loses nothing. `merged="false"` means it still
holds work the default branch doesn't have. `ahead="-1"` with
`status="unknown"` means the comparison failed — that branch is
deliberately never counted as merged, since that's the direction that
would talk someone into deleting unfinished work.

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

### Flow: is work closing, and is it closing by itself?

Everything above is a snapshot. These are computed from each issue's
`CreatedDate`/`ClosedDate` on every poll, so they carry full history from the
first scrape instead of only from the day you deployed.

| Metric | Meaning |
|---|---|
| `ado_flow_read_ok` | 1 if the flow half was computed. It is isolated, so a failure here never costs the board metrics |
| `ado_items_created_total{project}` / `ado_items_closed_total{project}` | Issues ever created / currently Done. `increase()` gives arrivals and departures |
| `ado_items_daily{project,kind,day}` | Issues `created` or `closed` on each UTC day, last `FLOW_DAYS` days |
| `ado_items_window{project,epic,kind,window}` | Created / closed per epic in the trailing `7d` and `30d` |
| `ado_cycle_time_days{project,window,quantile}` | Created-to-Done time (p50, p90) of issues closed in the window; `ado_cycle_time_count` is n |
| `ado_open_age_items{project,bucket}` | Open issues by age: `0-7d`, `7-30d`, `30-90d`, `90d+` |
| `ado_waiting_items{project,tag}` | Open issues carrying a waiting-on-owner tag (`WAITING_TAGS`) |
| `ado_waiting_age_days{project,tag,stat}` | `max` and `median` age of those |
| `ado_closed_by_need{project,window,needed_owner}` | Issues closed in the window, split by whether they **ever** carried a waiting tag. `needed_owner="false"` over the sum is the self-closing ratio |
| `ado_pr_merge_time_days{project,window,quantile}` | Open-to-merge time of completed ADO PRs; `ado_prs_merged_window`, `ado_prs_merged_daily` |
| `github_pr_merge_time_days{project,window,quantile}` | The same for GitHub (last 30 merged per repo); `project` is the owner |

`needed_owner` reads each closed issue's tag **history** (one `updates` call per
issue, cached per revision), because a waiting tag is usually removed when the
owner answers and the closing revision alone would under-count. It cannot see
who clicked: every change made through a PAT is recorded as the PAT's owner, so
tags are the only reliable signal of "this waited on a human".

`ado_work_item_info` carries the title as a label on purpose — that's what
makes a sortable, clickable table panel possible in Grafana, and it's safe
at ~100 items. If your board holds thousands, drop the title label and
join on `id` in Grafana instead.

Forks and archived GitHub repos are skipped by default (a fork carries its
upstream project's whole branch list, which isn't your work and drowns out
what is) — set `SKIP_FORKS=0` to include them.

## Config

| Env var | Default | Notes |
|---|---|---|
| `ADO_ORG` | *(required)* | e.g. `https://dev.azure.com/your-org` |
| `ADO_PAT_FILE` | `/run/secrets/ado-pat` | File holding a PAT scoped **Work Items: Read** (add **Code: Read** too for PR/branch metrics) |
| `GITHUB_OWNER` | *(unset)* | Optional — GitHub user/org to also watch. Leave unset to disable GitHub metrics entirely |
| `GITHUB_TOKEN_FILE` | `/run/secrets/github-token` | Only read if `GITHUB_OWNER` is set. Fine-grained PAT scoped Contents:Read, Pull requests:Read, Metadata:Read |
| `LISTEN_PORT` | `9823` | |
| `CACHE_SECONDS` | `120` | Work-item cache |
| `CODE_CACHE_SECONDS` | `600` | Branch/GitHub cache — that half is much more expensive to poll |
| `FLOW_DAYS` | `28` | Days of `*_daily` series |
| `WAITING_TAGS` | `blocked-on-you,needs-decision,hands-on` | Tags that mean an issue is waiting on its owner |
| `MAX_COMPARES` | `120` | Caps per-poll branch-compare calls so a repo that suddenly sprouts hundreds of branches can't turn one scrape into a rate-limit incident |

Both the ADO PAT and the GitHub token arrive as **read-only bind mounts**,
never environment variables — anything in a container's env is readable via
`docker inspect`.

### The failure mode worth knowing about

A GitHub token that can list repos but not read their contents does not
fail loudly. The listing looks healthy and every private repo reads as
zero branches and zero PRs — "0 unmerged branches" looks like good news
and actually means blindness. This exporter checks a contradiction instead:
a repo that has ever been pushed to has at least one branch, so
`pushedAt` set with no readable branches means `github_read_ok 0` /
`github_read_problem{reason="cannot_read_refs"}` rather than silently
reporting zero. If you hit it, your token needs the whole `repo` scope
(classic) or Contents+Pull requests+Metadata:Read (fine-grained) over the
repos in question.

## Tests

    python3 test_exporter.py

Offline, no network. Covers label escaping, `Done` exclusion from the open
table while still counting in totals, tag counts not being inflated by
closed items, parentless issues rolling up as `(no epic)`, age-in-days as
the series value, and the GitHub half: forks excluded, merged/unmerged
branch classification, and every GitHub failure mode reporting
`github_read_ok 0` rather than raising.

## Build

    docker build -t ado-exporter:latest .

## Run

    docker run -d --name ado-exporter --restart unless-stopped \
      -p 9823:9823 \
      -v /path/to/ado-pat:/run/secrets/ado-pat:ro \
      -e ADO_ORG=https://dev.azure.com/your-org \
      ado-exporter:latest

Scrape `:9823/metrics` with Prometheus.
