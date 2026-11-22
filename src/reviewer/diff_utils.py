"""Filter changed files, number diff lines, split into chunks. Pure functions, no AWS."""
import re

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
SKIP_NAMES = {"package-lock.json", "pnpm-lock.yaml", "Pipfile.lock"}
SKIP_DIRS = {"dist", "build", "vendor", "node_modules"}
SKIP_SUFFIX = (".lock", ".min.js", ".min.css", ".map", ".png", ".jpg", ".jpeg",
               ".gif", ".svg", ".ico", ".webp", ".pdf")


def skip_reason(f: dict):
    """f is one item of GitHub's pulls/{n}/files. Returns why to skip, or None."""
    name = f["filename"]
    if f.get("status") == "removed":
        return "removed"
    if not f.get("patch"):
        return "binary or too large"
    if name.rsplit("/", 1)[-1] in SKIP_NAMES or name.lower().endswith(SKIP_SUFFIX):
        return "lock/image/minified"
    if SKIP_DIRS & set(name.split("/")[:-1]):
        return "generated folder"
    return None


def number_lines(patch: str):
    """Returns (text with new-file line numbers, set of commentable line numbers).
    '+' and ' ' lines get the current number; '-' lines get none."""
    out, valid, n = [], set(), None
    for line in patch.splitlines():
        m = HUNK.match(line)
        if m:
            n = int(m.group(1))
            out.append(line)
        elif n is None or line.startswith("\\"):  # before first hunk / "No newline" marker
            continue
        elif line.startswith("-"):
            out.append(f"      {line}")
        else:
            out.append(f"L{n:<4} {line}")
            valid.add(n)
            n += 1
    return "\n".join(out), valid


def chunk(sections: list[str], max_tokens: int = 60_000) -> list[str]:
    """Greedy packing at ~4 chars/token. A section bigger than the budget is truncated."""
    limit = max_tokens * 4
    chunks, cur, size = [], [], 0
    for s in sections:
        if len(s) > limit:
            s = s[:limit] + "\n[truncated]"
        if cur and size + len(s) > limit:
            chunks.append("\n\n".join(cur))
            cur, size = [], 0
        cur.append(s)
        size += len(s)
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks
