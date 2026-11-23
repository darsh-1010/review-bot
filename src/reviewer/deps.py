"""Known-vulnerability check for dependencies using Google's free OSV.dev database (no API key).
parse() is pure; audit() calls the network and never raises (a failed lookup returns a note, not a crash)."""
import json
import posixpath
import re
import urllib.request

OSV = "https://api.osv.dev/v1"
MANIFEST_NAMES = {"requirements.txt", "package-lock.json", "package.json"}
SEV = {"CRITICAL": "high", "HIGH": "high", "MODERATE": "medium", "MEDIUM": "medium", "LOW": "low"}
MAX_PKGS, MAX_VULNS = 400, 30


def parse(manifests):
    """{path: text} -> sorted [(ecosystem, name, version, path)]. Only versions we can pin down."""
    pkgs = set()
    for path, text in manifests.items():
        base = posixpath.basename(path)
        if base.startswith("requirements") and base.endswith(".txt"):
            for line in text.splitlines():
                m = re.match(r"^\s*([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?\s*==\s*([A-Za-z0-9_.+!-]+)", line)
                if m:
                    pkgs.add(("PyPI", m.group(1), m.group(2), path))
        elif base == "package-lock.json":
            try:
                d = json.loads(text)
            except ValueError:
                continue
            for key, v in (d.get("packages") or {}).items():  # lockfile v2/v3
                if key.startswith("node_modules/") and v.get("version"):
                    pkgs.add(("npm", key.rsplit("node_modules/", 1)[1], v["version"], path))
            for name, v in (d.get("dependencies") or {}).items():  # lockfile v1
                if isinstance(v, dict) and v.get("version"):
                    pkgs.add(("npm", name, v["version"], path))
        elif base == "package.json":
            try:
                d = json.loads(text)
            except ValueError:
                continue
            for sect in ("dependencies", "devDependencies"):
                for name, ver in (d.get(sect) or {}).items():
                    v = re.sub(r"^[\^~=v]+", "", str(ver)).strip()
                    if re.match(r"^\d+\.\d+\.\d+$", v):  # exact or caret/tilde base: the declared minimum
                        pkgs.add(("npm", name, v, path))
    return sorted(pkgs)


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def _get(url):
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.load(r)


def _fixed(vuln, name):
    for a in vuln.get("affected", []):
        if a.get("package", {}).get("name", "").lower() == name.lower():
            for rng in a.get("ranges", []):
                for ev in rng.get("events", []):
                    if "fixed" in ev:
                        return ev["fixed"]
    return None


def audit(manifests):
    """-> (findings, note). Findings use the same shape as the scanners' issues."""
    pkgs = parse(manifests)[:MAX_PKGS]
    if not pkgs:
        return [], None
    try:
        res = _post(f"{OSV}/querybatch", {"queries": [
            {"package": {"ecosystem": e, "name": n}, "version": v} for e, n, v, _ in pkgs]})["results"]
        hits = [(p, [v["id"] for v in r.get("vulns", [])]) for p, r in zip(pkgs, res) if r.get("vulns")]
        out, fetched = [], 0
        for (eco, name, ver, path), ids in hits:
            for vid in ids[:2]:  # the two most relevant advisories per package is plenty
                if fetched >= MAX_VULNS:
                    break
                fetched += 1
                v = _get(f"{OSV}/vulns/{vid}")
                sev = SEV.get(str(v.get("database_specific", {}).get("severity", "")).upper(), "medium")
                fix = _fixed(v, name)
                out.append({"path": path, "line": 1, "severity": sev, "tool": "osv", "rule": vid, "category": "dependencies",
                            "owasp": "A03 Supply Chain",
                            "message": f"{name} {ver} has a known vulnerability ({vid}): {(v.get('summary') or 'see advisory')[:160]}",
                            "suggestion": f"Upgrade {name} to {fix} or later." if fix else f"Upgrade or replace {name}; see https://osv.dev/{vid}."})
        note = f"checked {len(pkgs)} pinned dependencies against OSV.dev: {len(out)} advisories"
        if len(parse(manifests)) > MAX_PKGS:
            note += f" (only the first {MAX_PKGS} were checked)"
        return out, note
    except Exception as e:  # noqa: BLE001 - OSV down must not fail a scan
        print("osv failed:", repr(e))
        return [], "dependency vulnerability check skipped (OSV.dev unreachable)"
