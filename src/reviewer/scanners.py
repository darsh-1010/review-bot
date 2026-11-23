"""Deterministic checks that run before Claude: bandit (Python security linter) + a secret scanner.
Pure functions, no AWS. A scanner that breaks must never break a review, so callers wrap these in try/except."""
import os
import re
import tempfile

# (name, pattern). Matched values are never copied into findings, only the rule name.
SECRET_PATTERNS = [
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{30,}\b")),
    ("API key (sk-...)", re.compile(r"\bsk-[A-Za-z0-9_-]{24,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("Private key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY")),
    ("Hardcoded credential", re.compile(
        r"""(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\b["']?\s*[:=]\s*["']([^"'\s]{8,})["']""")),
]
PLACEHOLDER = re.compile(r"(?i)example|your[_-]|changeme|placeholder|dummy|sample|xxxx|<.+>|\$\{|\{\{|test|todo|redacted|\*{4}")
SKIP_SECRET_SUFFIX = (".md", ".lock", ".svg", ".png", ".jpg", ".pdf", ".map")


def scan_secrets(files: dict[str, str]):
    """files: {path: text}. One finding per leaked-looking line."""
    out = []
    for path, text in files.items():
        if path.lower().endswith(SKIP_SECRET_SUFFIX):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if len(line) > 2000:  # minified or generated: not worth it
                continue
            for name, pat in SECRET_PATTERNS:
                m = pat.search(line)
                if not m:
                    continue
                if name == "Hardcoded credential" and PLACEHOLDER.search(m.group(1)):
                    continue
                out.append({"path": path, "line": n, "severity": "high", "tool": "secrets", "rule": name,
                            "message": f"Possible {name} committed in source. Rotate it, remove it from the code "
                                       f"and from git history, and load it from a secret store instead."})
                break  # one finding per line
    return out


def scan_bandit(files: dict[str, str]):
    """Runs bandit on the .py files. Returns [] if bandit isn't installed."""
    py = {p: t for p, t in files.items() if p.endswith(".py")}
    if not py:
        return []
    try:
        from bandit.core import config, manager
    except ImportError:
        return []
    with tempfile.TemporaryDirectory() as d:
        names = {}
        for i, (p, t) in enumerate(py.items()):  # flat temp names: repo paths never touch the filesystem
            f = os.path.join(d, f"f{i}.py")
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(t)
            names[f] = p
        mgr = manager.BanditManager(config.BanditConfig(), "file")
        mgr.discover_files(list(names))
        mgr.run_tests()
        return [{"path": names[i.fname], "line": i.lineno, "severity": i.severity.lower(), "tool": "bandit",
                 "rule": i.test_id, "message": i.text} for i in mgr.get_issue_list() if i.fname in names]


def scan(files: dict[str, str]):
    """Both scanners; each one failing only loses its own findings."""
    out = []
    for fn in (scan_secrets, scan_bandit):
        try:
            out += fn(files)
        except Exception as e:  # ponytail: log and move on, a scanner bug must not block a review
            print(f"{fn.__name__} failed:", repr(e))
    return out


def findings_text(findings: list[dict], limit: int = 25) -> str:
    """One line per finding, for the Claude prompt."""
    return "\n".join(f"- L{f['line']} [{f['tool']} {f['rule']}, {f['severity']}] {' '.join(f['message'].split())[:200]}"
                     for f in findings[:limit])
