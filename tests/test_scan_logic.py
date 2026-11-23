"""Pure parts of the repo scan: which files, the best-practice checklist, the score. (scan.py imports app, so load it with the env set.)"""
import importlib.util, os, pathlib, sys

os.environ.update(AWS_DEFAULT_REGION="ap-southeast-2", AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x",
                  TABLE_NAME="t", CLAUDE_MODEL="claude-haiku-5-5")
ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src/reviewer"))
import scan  # noqa: E402


def t(path, size=100):
    return {"path": path, "size": size}


def test_priority_puts_config_first_and_skips_junk():
    assert scan.priority("Dockerfile") == 0 and scan.priority(".github/workflows/ci.yml") == 0
    assert scan.priority("src/auth/login.py") == 1 and scan.priority("src/util.py") == 2
    assert scan.priority("tests/test_x.py") == 3
    assert all(scan.priority(p) is None for p in ["logo.png", "node_modules/x/index.js", "package-lock.json", "README.md", "dist/a.js"])


def test_select_respects_budgets_and_order():
    tree = [t("a.py"), t("Dockerfile"), t("big.py", 999_999), t("empty.py", 0), t("pic.png")]
    assert scan.select(tree) == (["Dockerfile", "a.py"], 0)
    many = [t(f"f{i}.py") for i in range(scan.MAX_FILES + 5)]
    picked, left = scan.select(many)
    assert len(picked) == scan.MAX_FILES and left == 5


def ok(checks):
    return {c["name"]: c["ok"] for c in checks}


def test_checklist_good_and_bad_repo():
    good = ["README.md", "LICENSE", ".gitignore", "tests/test_a.py", ".github/workflows/ci.yml", "SECURITY.md",
            ".github/dependabot.yml", "package.json", "package-lock.json", ".env.example"]
    assert all(ok(scan.checklist(good, {})).values())
    bad = ok(scan.checklist(["app.py", "package.json", ".env", "requirements.txt"], {"requirements.txt": "flask\nboto3==1.0\n"}))
    assert not any(bad[k] for k in ["README", "License", ".gitignore", "Automated tests", "CI pipeline", "Dependency lockfile",
                                    "Pinned Python requirements", "Dependency update bot", "Security policy", "No .env file committed"])


def test_score_floor_and_weights():
    one = lambda s: [{"severity": s}]  # noqa: E731
    assert scan.score(one("high"), []) == 88 and scan.score(one("low"), []) == 99
    assert scan.score([{"severity": "high"}] * 20, []) == 0
