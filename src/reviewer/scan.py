"""Whole-repo scan (dashboard "Repo scan" tab): SQS job -> GitHub tree + files -> bandit/secrets + Claude -> DynamoDB.
Runs as its own Lambda (long timeout) from the same code package as the PR reviewer."""
import datetime
import json
import posixpath
import time

import checks
import deps
import app  # reuses the reviewer's table, SSM token lookup, repo settings and Claude client
import claude_client as cc
import diff_utils as du
import github_client as gh
import scanners

SEV = {"high": 0, "medium": 1, "low": 2}
MAX_FILES, MAX_BYTES, MAX_FILE_BYTES = 80, 400_000, 100_000
MAX_ISSUES = 120
SOURCE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".rb", ".php", ".cs", ".rs", ".kt", ".swift",
              ".c", ".cc", ".cpp", ".h", ".sh", ".sql", ".tf", ".yml", ".yaml", ".json", ".toml", ".html"}
RISKY_NAMES = ("auth", "login", "password", "secret", "token", "crypto", "session", "config", "settings",
               "admin", "payment", "upload", "webhook", "handler", "app", "main", "server", "index", "template")
MANIFESTS = {"requirements.txt", "package.json", "pyproject.toml", "Pipfile", "go.mod", "Cargo.toml", "Gemfile", "pom.xml"}
LOCKFILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", "go.sum", "Cargo.lock",
             "Gemfile.lock", "uv.lock"}
EXAMPLE_ENV = (".example", ".sample", ".template", ".dist")


def priority(path: str):
    """Lower = scanned first. None = not worth sending to Claude."""
    low, name = path.lower(), posixpath.basename(path).lower()
    ext = posixpath.splitext(name)[1]
    if name in LOCKFILES:
        return None
    if name in du.SKIP_NAMES or low.endswith(du.SKIP_SUFFIX) or du.SKIP_DIRS & set(low.split("/")[:-1]):
        return None
    if (name == "dockerfile" or name.startswith(".env") or ext == ".tf" or low.startswith(".github/workflows/")
            or name in {"requirements.txt", "package.json", "docker-compose.yml", "docker-compose.yaml", "template.yaml"}):
        return 0                                           # config and infrastructure: where the worst mistakes live
    if ext not in SOURCE_EXT:
        return None
    if "test" in low or "spec" in low or "/docs/" in low:
        return 3
    return 1 if any(w in name for w in RISKY_NAMES) else 2


def select(tree: list[dict]):
    """Pick what to scan within the file and size budgets. Returns (paths, how many candidates were left out)."""
    cand = sorted((priority(t["path"]), t["size"], t["path"]) for t in tree
                  if 0 < t["size"] <= MAX_FILE_BYTES and priority(t["path"]) is not None)
    picked, total = [], 0
    for _, size, path in cand:
        if len(picked) >= MAX_FILES or total + size > MAX_BYTES:
            continue
        picked.append(path)
        total += size
    return picked, len(cand) - len(picked)


def checklist(paths: list[str], texts: dict[str, str]):
    """Repo-level best practices, judged from the file list alone: [{name, ok, hint}]."""
    names = {posixpath.basename(p) for p in paths}
    lower = {p.lower() for p in paths}
    lbase = {posixpath.basename(p).lower() for p in paths}
    has = lambda *n: any(x.lower() in lbase for x in n)  # noqa: E731
    pins = [t for p, t in texts.items() if posixpath.basename(p).startswith("requirements") and p.endswith(".txt")]
    env_leak = [p for p in paths if posixpath.basename(p).startswith(".env")
                and not posixpath.basename(p).endswith(EXAMPLE_ENV)]
    manifests = names & MANIFESTS
    unlocked = bool(manifests - {"requirements.txt"}) and not names & LOCKFILES
    unpinned = bool(pins) and any(l.strip() and not l.startswith(("#", "-")) and "==" not in l for t in pins for l in t.splitlines())
    items = [
        ("README", has("readme.md", "readme.rst", "readme"), "Add a README: what it does, how to run and deploy it."),
        ("License", has("license", "license.md", "license.txt", "copying"), "Add a LICENSE so others know how they may use the code."),
        (".gitignore", ".gitignore" in names, "Add a .gitignore so build output and secrets are not committed."),
        ("Automated tests", any("test" in p.lower() for p in paths), "Add automated tests and run them on every pull request."),
        ("CI pipeline", any(p.startswith(".github/workflows/") for p in paths) or has(".gitlab-ci.yml", "jenkinsfile"),
         "Run tests and linters in CI (e.g. GitHub Actions) on every pull request."),
        ("Dependency lockfile", not unlocked, "Commit a lockfile so builds use the same dependency versions."),
        ("Pinned Python requirements", not unpinned, "Pin requirements with == (or use a lockfile) so builds are repeatable."),
        ("Dependency update bot", ".github/dependabot.yml" in lower or ".github/dependabot.yaml" in lower or has("renovate.json"),
         "Enable Dependabot or Renovate to get security updates automatically."),
        ("Security policy", has("security.md"), "Add SECURITY.md: how to report a vulnerability."),
        ("No .env file committed", not env_leak, "Remove " + ", ".join(env_leak[:3]) + " from git and rotate any secrets inside."),
    ]
    return [{"name": n, "ok": bool(ok), "hint": h} for n, ok, h in items]


def near(issues, f):
    """Did Claude already report this finding (same file, within 2 lines)?"""
    return any(i["path"] == f["path"] and abs(i["line"] - f["line"]) <= 2 for i in issues)


def score(issues: list[dict], checks: list[dict]):
    """0-100 at a glance. Deliberately simple: -12 per high, -4 per medium, -1 per low, -3 per failed check."""
    n = lambda s: sum(i["severity"] == s for i in issues)  # noqa: E731
    return max(0, 100 - 12 * n("high") - 4 * n("medium") - n("low") - 3 * sum(not c["ok"] for c in checks))


def _set(sk, **fields):
    names = {f"#{k}": k for k in fields}
    vals = {f":{k}": v for k, v in fields.items()}
    app.table.update_item(Key={"pk": "SCAN", "sk": sk}, ExpressionAttributeNames=names, ExpressionAttributeValues=vals,
                          UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields))


def handler(event, context):
    for rec in event["Records"]:
        job = json.loads(rec["body"])
        try:
            run(job, context)
        except Exception as e:  # never re-raise: SQS would retry and pay for Claude again
            print("scan failed:", repr(e))
            _set(job["sk"], status="failed", error=str(e)[:300] or type(e).__name__)


def run(job, context):
    sk, repo = job["sk"], job["repo"]
    row = app.table.get_item(Key={"pk": "SCAN", "sk": sk}).get("Item")
    if not row or row["status"] != "queued":  # duplicate delivery
        return
    _set(sk, status="running")
    t0 = time.time()
    left = context.get_remaining_time_in_millis
    model = job["model"] if job["model"] in cc.PRICES else app.MODEL
    cap = int(job["max_cost_micro"])
    _, _, account = app.repo_settings(repo)
    token = app._gh_token(account)

    info = gh.get_repo(repo, token)
    branch = info["default_branch"]
    tree, truncated = gh.get_tree(repo, branch, token)
    paths, left_out = select(tree)

    texts, notes = {}, []
    for p in paths:
        if left() < 240_000:  # leave time for Claude and the save
            notes.append("ran out of time while downloading files")
            break
        text = gh.get_file(repo, p, branch, token, MAX_FILE_BYTES)
        if text is not None:
            texts[p] = text

    manifests = {}  # dependency files, checked against OSV.dev (never sent to Claude)
    for t in [t for t in tree if posixpath.basename(t["path"]) in deps.MANIFEST_NAMES and priority_dir_ok(t["path"])][:10]:
        if left() > 300_000 and t["size"] <= 1_500_000:
            text = gh.get_file(repo, t["path"], branch, token, 1_500_000)
            if text:
                manifests[t["path"]] = text

    ptype = job.get("project_type", "auto")
    detected = ptype not in checks.PROJECT_TYPES or ptype == "auto"
    if detected:
        ptype = checks.detect_type({**texts, **manifests})
    packs = checks.PACKS.get(ptype, set()) | {"generic"}

    found = scanners.scan(texts, packs)
    prompts = checks.extract_prompts(texts) if ptype == "ai" else []
    found += checks.prompt_secret_findings(prompts)
    secrets = [f for f in found if f["tool"] == "secrets"]
    by_file = {}
    for f in found:
        if f["tool"] != "secrets":
            by_file.setdefault(f["path"], []).append(f)

    sections = []
    for p in sorted(texts, key=lambda p: (priority(p), p)):
        body = "\n".join(f"L{n} {line}" for n, line in enumerate(texts[p].splitlines(), 1))
        sec = f"### {p}\n{body}"
        if p in by_file:
            sec += f"\n\n#### Scanner findings\n{scanners.findings_text(by_file[p])}"
        sections.append(sec)

    summaries, issues, tin, tout = [], [], 0, 0
    for c in du.chunk(sections, 30_000):
        if left() < 100_000:
            notes.append("ran out of time: part of the files was not analysed")
            break
        if cc.cost_micro(model, tin, tout) + cc.estimate_micro(model, c) > cap:
            notes.append(f"cost cap ${cap / 1e6:.2f} reached: part of the files was not analysed")
            break
        out, usage = cc.scan_chunk(app._claude(), model, c, ptype)
        summaries.append(out["summary"])
        issues += out["issues"]
        tin, tout = tin + usage.input_tokens, tout + usage.output_tokens

    review, test_yaml = [], ""
    if prompts and left() > 90_000:  # grade the system prompt(s) and draft a starter test file
        body = "\n".join(p["text"] for p in prompts)
        if cc.cost_micro(model, tin, tout) + cc.cost_micro(model, len(body) // 4 + 2000, 8000) > cap:
            notes.append("cost cap reached: prompt review skipped")
        else:
            try:
                out, usage = cc.prompt_review(app._claude(), model, prompts)
                review, test_yaml = out["prompts"], out["test_yaml"][:8000]
                tin, tout = tin + usage.input_tokens, tout + usage.output_tokens
            except Exception as e:  # noqa: BLE001 - the rest of the scan is still worth saving
                print("prompt review failed:", repr(e))
                notes.append("prompt review failed; the rest of the scan is complete")

    direct = list(secrets) + [f for f in found if f["tool"] == "rules" and f["severity"] == "high"]
    if manifests:
        osv, note = deps.audit(manifests)
        direct += osv
        notes += [note] if note and (osv or "skipped" in note) else []
    if left() > 120_000:  # secrets removed from the code but still in git history
        try:
            known = {(f["path"], f["rule"]) for f in secrets}
            for sha, added in gh.get_commit_patches(repo, token, 25):
                for f in scanners.scan_secrets(added):
                    if (f["path"], f["rule"]) not in known:
                        known.add((f["path"], f["rule"]))
                        direct.append({**f, "line": 1, "commit": sha,
                                       "message": f"{f['rule']} found in git history (commit {sha[:7]}). Even if deleted since, it is still readable."})
        except Exception as e:  # noqa: BLE001
            print("history scan failed:", repr(e))
            notes.append("git history scan skipped")
    claude, injected = list(issues), set()
    for f in direct:  # deterministic findings are always reported, even if Claude missed them or the cap stopped it
        if not near(claude, f) and (f["path"], f["line"]) not in injected:
            injected.add((f["path"], f["line"]))
            issues.append({"path": f["path"], "line": f["line"], "severity": f["severity"],
                           "category": f.get("category", "security"), "message": f["message"],
                           "suggestion": f.get("suggestion", "Revoke the key, then load it from a secret store such as SSM or Secrets Manager."),
                           **({"owasp": f["owasp"]} if f.get("owasp") else {}), **({"commit": f["commit"]} if f.get("commit") else {})})
    issues.sort(key=lambda i: SEV[i["severity"]])
    issues = issues[:MAX_ISSUES]
    checks_list = checklist([t["path"] for t in tree], texts)
    if ptype == "ai":
        checks_list += [{"name": n, "ok": bool(ok), "hint": h} for n, ok, h in checks.ai_checklist(found, texts)]
    if truncated:
        notes.append("GitHub returned a partial file list (very large repo)")
    too_big = sum(t["size"] > MAX_FILE_BYTES and priority(t["path"]) is not None for t in tree)
    if too_big:
        notes.append(f"{too_big} source files bigger than {MAX_FILE_BYTES // 1000} KB were skipped")
    if not texts:
        notes.append("no scannable source files found (only the repo-level checklist was evaluated)")
    if left_out:
        notes.append(f"{left_out} lower-priority files were not scanned (limits: {MAX_FILES} files, {MAX_BYTES // 1000} KB)")

    _set(sk, status="done", branch=branch, model=model, summaries=summaries[:8], issues=issues, checks=checks_list,
         score=score(issues, checks_list), files_scanned=len(texts), files_total=len(tree), notes=notes,
         project_type=ptype, detected=detected, prompts=review, test_yaml=test_yaml,
         bandit_findings=sum(f["tool"] == "bandit" for f in found), input_tokens=tin, output_tokens=tout,
         cost_micro=cc.cost_micro(model, tin, tout), duration_ms=int((time.time() - t0) * 1000),
         finished=datetime.datetime.now(datetime.timezone.utc).isoformat())


def priority_dir_ok(path):
    """Skip vendored copies (node_modules, venv...) when looking for dependency manifests."""
    return not (du.SKIP_DIRS & set(path.lower().split("/")[:-1]))
