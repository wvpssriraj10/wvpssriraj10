#!/usr/bin/env python3
"""Refresh data/contributions.json and data/repos.json from the GitHub API.

Needs a token in GH_TOKEN or GITHUB_TOKEN. The built-in Actions token only sees
public contributions; to match the count on your profile (private included),
save a personal access token with `read:user` as the repo secret PROFILE_TOKEN.
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
USER = CFG["user"]
TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
if not TOKEN:
    sys.exit("Set GH_TOKEN or GITHUB_TOKEN")


def call(url, body=None):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {TOKEN}", "User-Agent": "profile-cards",
                 "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


QUERY = """query($login:String!){user(login:$login){contributionsCollection{contributionCalendar{
totalContributions weeks{contributionDays{date contributionCount}}}}}}"""
res = call("https://api.github.com/graphql", {"query": QUERY, "variables": {"login": USER}})
if "errors" in res:
    sys.exit(f"GraphQL error: {res['errors']}")
cal = res["data"]["user"]["contributionsCollection"]["contributionCalendar"]
days = [{"date": d["date"], "count": d["contributionCount"]}
        for w in cal["weeks"] for d in w["contributionDays"]]
(ROOT / "data").mkdir(exist_ok=True)
(ROOT / "data" / "contributions.json").write_text(
    json.dumps({"user": USER, "total": cal["totalContributions"], "days": days}), encoding="utf-8")

repos = {}
for item in CFG["featured"]:
    r = call(f"https://api.github.com/repos/{USER}/{item['repo']}")
    repos[item["repo"]] = {"description": r.get("description"), "language": r.get("language"),
                           "stars": r["stargazers_count"], "forks": r["forks_count"],
                           "pushed_at": r["pushed_at"]}
(ROOT / "data" / "repos.json").write_text(json.dumps(repos, indent=1), encoding="utf-8")
print(f"{cal['totalContributions']} contributions, {len(days)} days, {len(repos)} repos")
