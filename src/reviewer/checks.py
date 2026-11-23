"""Deterministic rule checks (SQL injection, XSS, risky config, AI-app risks...). Pure functions, no AWS, no network.
Line-based regexes: cheap and predictable. Claude triages the findings afterwards, so a few false positives are fine.
Rule packs: "generic" always runs; "web" and "ai" run for those project types; "infra" is path-gated so it always runs."""
import posixpath
import re

PROJECT_TYPES = ("auto", "web", "ai", "infra", "library")
PACKS = {"web": {"web"}, "ai": {"ai", "web"}, "infra": set(), "library": set()}
ALL_PACKS = {"generic", "web", "ai", "infra"}

CODE = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".java", ".go", ".php", ".rb", ".cs", ".kt"}
LLM_IMPORT = re.compile(r"\b(anthropic|openai|langchain\w*|litellm|google\.generativeai|google\.genai|ollama|cohere|"
                        r"mistralai|llama_index|bedrock-runtime|invoke_model)\b|@anthropic-ai/sdk|ai-sdk")
LLM_CALL = re.compile(r"\.(messages\.create|chat\.completions\.create|completions\.create|generate_content|converse|"
                      r"invoke_model|generateText|streamText)\s*\(")
LIMIT_WORDS = re.compile(r"max_tokens|max_output_tokens|max_completion_tokens|maxTokens|maxOutputTokens|max_new_tokens")
WEB_WORDS = re.compile(r"\b(flask|django|fastapi|express|koa|spring|rails|laravel|nextjs|next/|react|vue|svelte)\b|<html", re.I)
RATE_WORDS = re.compile(r"ratelimit|rate_limit|rate-limit|limiter|throttle|slowapi|quota", re.I)
PROMPT_HINT = re.compile(r"you are|your task|system prompt|assistant|instructions?:", re.I)
FAIL = {"high": 0, "medium": 1, "low": 2}


def R(id, pack, exts, pat, sev, cat, owasp, msg, fix, flags=0):
    return dict(id=id, pack=pack, exts=exts, pat=re.compile(pat, flags), sev=sev, cat=cat, owasp=owasp, msg=msg, fix=fix)


PY, JS = {".py"}, {".js", ".jsx", ".ts", ".tsx", ".mjs"}
RULES = [
    R("SQLI-PY", "generic", PY, r"""\b(execute|executemany|raw|read_sql\w*|text)\s*\(\s*(f["']|["'][^"']*["']\s*(%|\+|\.format\())""",
      "high", "security", "A05 Injection", "SQL query built with string formatting: SQL injection.",
      "Use parameterised queries, e.g. cursor.execute('... WHERE id = %s', (value,))."),
    R("SQLI-JS", "generic", JS, r"""\.(query|execute|raw)\s*\(\s*(`[^`]*\$\{|["'][^"']*["']\s*\+)""",
      "high", "security", "A05 Injection", "SQL query built from a template string or concatenation: SQL injection.",
      "Use placeholders (db.query('... WHERE id = $1', [id])) or an ORM's bound parameters."),
    R("SQLI-PHP", "generic", {".php"}, r"""(mysqli?_query|->query)\s*\([^;]*\$_(GET|POST|REQUEST|COOKIE)|["']\s*(SELECT|INSERT|UPDATE|DELETE)\b[^"']*\$\w+""",
      "high", "security", "A05 Injection", "SQL built with request data or a PHP variable: SQL injection.",
      "Use PDO prepared statements with bound parameters.", re.I),
    R("SQLI-JVM-GO", "generic", {".java", ".cs", ".go", ".kt"},
      r"""(executeQuery|executeUpdate|createStatement|\.Query|\.Exec|SqlCommand)\w*\(\s*"[^"]*"\s*\+|fmt\.Sprintf\(\s*"\s*(SELECT|INSERT|UPDATE|DELETE)""",
      "high", "security", "A05 Injection", "SQL assembled by concatenation or Sprintf: SQL injection.",
      "Use PreparedStatement / parameterised queries."),
    R("CMD-INJECT", "generic", PY | JS, r"""os\.system\(|os\.popen\(|subprocess\.\w+\([^)]*shell\s*=\s*True|\b(exec|execSync)\s*\(\s*(`[^`]*\$\{|[^)'"`]*\+)""",
      "high", "security", "A05 Injection", "Shell command built from variables: command injection.",
      "Pass an argument list without shell=True (execFile in Node) and validate inputs."),
    R("EVAL", "generic", PY | JS, r"""(?<![\w.])(eval|exec)\s*\(|new Function\(""",
      "high", "security", "A05 Injection", "eval/exec runs dynamic code.", "Remove it; parse data with json/ast.literal_eval or a lookup table."),
    R("DESERIALIZE", "generic", PY, r"""pickle\.loads?\(|marshal\.loads\(|yaml\.load\((?![^)]*Loader\s*=\s*(yaml\.)?(Safe|CSafe))""",
      "high", "security", "A08 Data Integrity", "Unsafe deserialisation can run attacker code.", "Use json, or yaml.safe_load; never unpickle untrusted data."),
    R("TLS-OFF", "generic", CODE, r"""verify\s*=\s*False|rejectUnauthorized\s*:\s*false|InsecureSkipVerify\s*:\s*true|NODE_TLS_REJECT_UNAUTHORIZED""",
      "medium", "security", "A02 Misconfiguration", "TLS certificate checking is turned off.", "Keep verification on; trust a private CA instead if needed."),
    R("WEAK-HASH", "generic", CODE, r"""hashlib\.(md5|sha1)\(|createHash\(\s*["'](md5|sha1)""",
      "low", "security", "A04 Cryptographic Failures", "MD5/SHA-1 are broken for security use.", "Use SHA-256+, and bcrypt/argon2 for passwords."),
    R("DEBUG-ON", "generic", CODE, r"""\bdebug\s*=\s*True|\bDEBUG\s*=\s*True""",
      "medium", "config", "A02 Misconfiguration", "Debug mode enabled in code.", "Read it from an environment variable and keep it off in production."),
    R("JWT-WEAK", "generic", CODE, r"""algorithms?\s*[=:]\s*\[?\s*["']none|verify_signature["']?\s*:\s*False|jwt\.decode\([^)]*verify\s*=\s*False""",
      "high", "security", "A07 Authentication", "JWT signature is not verified.", "Verify with a fixed algorithm allow-list and a real key.", re.I),
    R("PATH-TRAVERSAL", "generic", PY | JS, r"""\bopen\(\s*(request|req)\.|send_file\(\s*(request|req)\.|readFile(Sync)?\(\s*(req|request)\.""",
      "high", "security", "A01 Access Control", "A file path comes straight from the request: path traversal.", "Join to a fixed base directory, resolve it, and reject paths that escape it."),
    R("SSRF", "generic", PY | JS, r"""requests\.(get|post|put)\(\s*(request|req)\.|urlopen\(\s*(request|req)\.|\bfetch\(\s*(req|request)\.""",
      "medium", "security", "A01 Access Control (SSRF)", "The server fetches a URL supplied by the user: SSRF.", "Allow-list hosts and block private/metadata IP ranges."),
    R("XSS", "web", CODE | {".html", ".vue", ".erb"}, r"""\.innerHTML\s*=|dangerouslySetInnerHTML|document\.write\(|v-html|\|\s*safe\b|mark_safe\(|\bMarkup\(""",
      "medium", "security", "A05 Injection (XSS)", "Raw HTML is inserted into the page: cross-site scripting if any part is user data.", "Use textContent / auto-escaping templates and sanitise with DOMPurify when HTML is required."),
    R("CORS-ANY", "web", CODE, r"""Access-Control-Allow-Origin["']?\s*[:,]\s*["']\*|allow_origins\s*=\s*\[\s*["']\*|origin\s*:\s*["']\*|\bCORS\(\s*app\s*\)""",
      "medium", "config", "A02 Misconfiguration", "CORS allows every origin.", "List the exact origins that need access."),
    R("COOKIE-FLAGS", "web", CODE, r"""httponly\s*[=:]\s*(False|false)|secure\s*[=:]\s*(False|false)""",
      "medium", "security", "A07 Authentication", "Cookie flag HttpOnly/Secure is explicitly off.", "Set HttpOnly, Secure and SameSite on session cookies.", re.I),
    # ---- AI / LLM apps (OWASP LLM Top 10) ----
    R("AI-PROMPT-INPUT", "ai", CODE, r"""(system\w*|SYSTEM\w*)\s*[=:]\s*(f["'][^"'\n]*\{(user|query|message|input|question|request|text|body|prompt)\w*|`[^`\n]*\$\{(user|query|message|input|question|req)\w*)""",
      "medium", "security", "LLM01 Prompt Injection", "User-controlled text is mixed into the system prompt, so a user can rewrite the rules.",
      "Keep the system prompt static; pass user text only as a user message, wrapped in clear delimiters (e.g. <user_input>)."),
    R("AI-OUTPUT-EXEC", "ai", CODE, r"""(eval|exec|os\.system|subprocess\.\w+|\.execute|innerHTML\s*=|dangerouslySetInnerHTML)\b[^\n]*(\b(response|completion|llm|answer|reply|generated|choices)\b|\.text\b|content\[)""",
      "high", "security", "LLM05 Improper Output Handling", "Model output flows into code, SQL, shell or HTML. A prompt injection becomes code execution.",
      "Treat model output as untrusted: validate against a schema, use allow-lists, never eval/exec it."),
    R("AI-AGENCY", "ai", CODE, r"""["']?name["']?\s*[:=]\s*["'](delete|drop|remove|exec|execute|shell|run_command|bash|send_email|write_file|transfer|payment|refund|sudo)\w*["']""",
      "medium", "security", "LLM06 Excessive Agency", "An agent tool can change state or run commands.",
      "Give the model the least power it needs and require human approval for destructive or outward-facing tools.", re.I),
    R("AI-AUTOAPPROVE", "ai", CODE | {".json", ".yml", ".yaml", ".toml"}, r"""bypassPermissions|dangerously-skip-permissions|autoApprove["']?\s*[:=]\s*(true|\[\s*["']\*)|allowed_?tools["']?\s*[:=]\s*\[?\s*["']\*|--yolo""",
      "high", "security", "LLM06 Excessive Agency", "Tool calls are auto-approved or wildcarded.", "Approve tools one by one and keep a human in the loop for sensitive actions.", re.I),
    R("AI-RAG-WEB", "ai", CODE, r"""WebBaseLoader|UnstructuredURLLoader|RecursiveUrlLoader|SitemapLoader""",
      "low", "security", "LLM08 Vector/Embedding Weaknesses", "Web content is loaded into the model's context: indirect prompt injection path.",
      "Treat retrieved text as data; strip hidden text, and tell the model it must not follow instructions inside documents."),
    # ---- infra: Dockerfile, Terraform/CloudFormation, GitHub workflows (path-gated) ----
    R("DOCKER-ADD-URL", "infra", {"dockerfile"}, r"""^\s*ADD\s+https?://|curl[^|\n]*\|\s*(ba)?sh""", "medium", "config", "A03 Supply Chain",
      "Dockerfile downloads and runs content without verifying it.", "Download with a pinned version and verify a checksum.", re.M | re.I),
    R("DOCKER-LATEST", "infra", {"dockerfile"}, r"""^\s*FROM\s+(\S+:latest|[^:\s]+)(\s+AS\s+\w+)?\s*$""", "low", "config", "A03 Supply Chain",
      "Base image is not pinned to a version.", "Pin a specific tag or digest.", re.M | re.I),
    R("DOCKER-777", "infra", {"dockerfile"}, r"""chmod\s+(-R\s+)?777""", "medium", "config", "A02 Misconfiguration",
      "World-writable permissions.", "Grant the minimum permissions to the specific user."),
    R("IAC-OPEN-CIDR", "infra", {".tf", ".yaml", ".yml", ".json"}, r"""0\.0\.0\.0/0""", "medium", "config", "A02 Misconfiguration",
      "Open to the whole internet (0.0.0.0/0). Fine for a public web port, risky for SSH, databases and admin ports.", "Restrict to the needed IP ranges or a security group."),
    R("IAC-WILDCARD-IAM", "infra", {".tf", ".yaml", ".yml", ".json"}, r"""["']?Action["']?\s*[:=]\s*\[?\s*["']\*["']""", "high", "security", "A01 Access Control",
      "IAM policy allows every action (*).", "List only the actions required (least privilege)."),
    R("IAC-PUBLIC-STORAGE", "infra", {".tf", ".yaml", ".yml", ".json"},
      r"""acl\s*=\s*["']public-read|AccessControl:\s*PublicRead|publicly_accessible\s*=\s*true|(encrypted|StorageEncrypted)["']?\s*[:=]\s*false""",
      "high", "security", "A02 Misconfiguration", "Storage or database is public or unencrypted.", "Make it private and turn encryption on.", re.I),
    R("GHA-SCRIPT-INJECTION", "infra", {".github/workflows"}, r"""\$\{\{\s*github\.event\.(issue|pull_request|comment|review|head_commit)[^}]*\.(title|body|message|name|ref|label)""",
      "high", "security", "A05 Injection", "Untrusted GitHub event text is placed in a workflow expression: script injection.",
      "Pass it through an env: variable and quote it in the shell."),
    R("GHA-PR-TARGET", "infra", {".github/workflows"}, r"""\bpull_request_target\b""", "medium", "security", "A08 Data Integrity",
      "pull_request_target runs with secrets on code from forks.", "Use pull_request, or never check out or run the PR's code in that workflow."),
    R("GHA-MOVING-REF", "infra", {".github/workflows"}, r"""uses:\s*[\w./-]+@(main|master|latest)\b""", "medium", "security", "A03 Supply Chain",
      "Action pinned to a moving branch.", "Pin to a release tag or commit SHA."),
]


def _scope(rule, path):
    """Does this rule apply to this file path?"""
    low, name = path.lower(), posixpath.basename(path).lower()
    ext = posixpath.splitext(name)[1]
    if rule["pack"] == "infra":
        keys = rule["exts"]
        if ".github/workflows" in keys:
            return low.startswith(".github/workflows/") and ext in (".yml", ".yaml")
        return name in keys or ext in keys or (name == "dockerfile" and "dockerfile" in keys)
    return ext in rule["exts"]


def _finding(path, line, rule):
    return {"path": path, "line": line, "severity": rule["sev"], "tool": "rules", "rule": rule["id"],
            "category": rule["cat"] if rule["cat"] in ("security", "config") else "security",
            "owasp": rule["owasp"], "message": rule["msg"], "suggestion": rule["fix"]}


def uses_llm(text):
    return bool(LLM_IMPORT.search(text))


def run_rules(files, packs=None):
    """files {path: text}. packs: set of pack names, None = all. One finding per (path, line, rule)."""
    packs = ALL_PACKS if packs is None else set(packs) | {"generic", "infra"}
    out = []
    for path, text in files.items():
        low = path.lower()
        if "test" in low or "/docs/" in low or "/examples/" in low:  # tests and samples are full of intentional bad code
            continue
        lines = text.splitlines()
        llm = "ai" in packs and posixpath.splitext(low)[1] in CODE and uses_llm(text)
        seen = set()
        for rule in RULES:
            if rule["pack"] not in packs or not _scope(rule, path):
                continue
            if rule["pack"] == "ai" and not llm and rule["id"] != "AI-AUTOAPPROVE":
                continue  # AI rules only for files that actually use an LLM
            if rule["pat"].flags & re.M:  # multi-line rules run on the whole text
                for m in rule["pat"].finditer(text):
                    ln = text.count("\n", 0, m.start()) + 1
                    if (ln, rule["id"]) not in seen:
                        seen.add((ln, rule["id"]))
                        out.append(_finding(path, ln, rule))
                continue
            for n, line in enumerate(lines, 1):
                if len(line) < 2000 and rule["pat"].search(line) and (n, rule["id"]) not in seen:
                    if rule["id"] == "DEBUG-ON" and "os.environ" in line:
                        continue
                    seen.add((n, rule["id"]))
                    out.append(_finding(path, n, rule))
        if llm:  # LLM call with no output limit: look 15 lines ahead for max_tokens
            for n, line in enumerate(lines, 1):
                if LLM_CALL.search(line) and not LIMIT_WORDS.search("\n".join(lines[n - 1:n + 15])):
                    out.append({"path": path, "line": n, "severity": "medium", "tool": "rules", "rule": "AI-NO-MAX-TOKENS",
                                "category": "reliability", "owasp": "LLM10 Unbounded Consumption",
                                "message": "LLM call has no max output token limit: one request can run up a large bill.",
                                "suggestion": "Set max_tokens on every call and add per-user rate limits and a spend budget."})
    out.sort(key=lambda f: FAIL[f["severity"]])
    return out


def extract_prompts(files, limit=3):
    """Find system-prompt-like strings: [{path, line, text}], at most `limit`, longest first (cut at 6000 chars)."""
    found = []
    for path, text in files.items():
        low, ext = path.lower(), posixpath.splitext(path.lower())[1]
        if "test" in low:
            continue
        if "prompt" in posixpath.basename(low) and ext in (".txt", ".md", ".yaml", ".yml", ".j2", ".jinja", ".prompt") and PROMPT_HINT.search(text):
            found.append({"path": path, "line": 1, "text": text[:6000]})
        elif ext in CODE and uses_llm(text):
            for m in re.finditer(r'("""|\'\'\')(.*?)\1|`([^`]{200,})`', text, re.S):
                body = m.group(2) or m.group(3) or ""
                if len(body) >= 150 and PROMPT_HINT.search(body):
                    found.append({"path": path, "line": text.count("\n", 0, m.start()) + 1, "text": body[:6000]})
            for m in re.finditer(r'(?m)^\s*[A-Z_]*PROMPT\w*\s*=\s*["\'](.{120,})["\']\s*$', text):
                found.append({"path": path, "line": text.count("\n", 0, m.start()) + 1, "text": m.group(1)[:6000]})
    found.sort(key=lambda p: -len(p["text"]))
    return found[:limit]


def prompt_secret_findings(prompts):
    """A prompt is visible to anyone who can extract it, so secrets in one are always a leak (LLM02/LLM07)."""
    pat = re.compile(r"(?i)password\s*[:=]|api[_ -]?key\s*[:=]|secret\s*[:=]|\b(postgres|mysql|mongodb(\+srv)?)://\S+:\S+@|\bbearer\s+[A-Za-z0-9._-]{20,}")
    out = []
    for p in prompts:
        for off, line in enumerate(p["text"].splitlines()):
            if pat.search(line):
                out.append({"path": p["path"], "line": p["line"] + off, "severity": "high", "tool": "rules", "rule": "AI-PROMPT-SECRET",
                            "category": "security", "owasp": "LLM07 System Prompt Leakage",
                            "message": "A credential or connection string appears inside a prompt. Anyone who gets the model to repeat its prompt gets the secret.",
                            "suggestion": "Remove it from the prompt; fetch secrets in code and keep the model away from them."})
    return out


def detect_type(files):
    """Best guess of the project type from file paths and contents."""
    texts = [t for p, t in files.items() if "test" not in p.lower()]
    if any(uses_llm(t) for t in texts):
        return "ai"
    names = list(files)
    iac = sum(p.endswith(".tf") or p.lower().endswith("dockerfile") or p.startswith(".github/") or p.endswith("template.yaml") for p in names)
    code = sum(posixpath.splitext(p)[1] in CODE for p in names)
    if iac and iac >= code:
        return "infra"
    if any(WEB_WORDS.search(t) for t in texts) or any(p.endswith(".html") for p in names):
        return "web"
    return "library"


def ai_checklist(findings, texts):
    """Repo-level AI-app checks, judged from rule findings and a keyword search."""
    has = lambda rule: any(f["rule"] == rule for f in findings)  # noqa: E731
    return [
        ("Every LLM call has a max-token limit", not has("AI-NO-MAX-TOKENS"), "Set max_tokens on every model call."),
        ("Model output is never executed", not has("AI-OUTPUT-EXEC"), "Validate model output and never pass it to eval, shell, SQL or HTML."),
        ("User text kept out of the system prompt", not has("AI-PROMPT-INPUT"), "Send user text only as a user message inside delimiters."),
        ("No secrets inside prompts", not has("AI-PROMPT-SECRET"), "Move credentials out of prompt text."),
        ("Agent tools are least-privilege", not (has("AI-AGENCY") or has("AI-AUTOAPPROVE")), "Require approval for destructive or outward-facing tools."),
        ("Rate limiting or spend quota present", any(RATE_WORDS.search(t) for t in texts.values()), "Add per-user rate limits and a spend budget to stop abuse and runaway cost."),
    ]
