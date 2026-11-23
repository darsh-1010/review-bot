import pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src/reviewer"))
import scanners  # noqa: E402

KEY = "AKIA" + "ABCDEFGHIJKLMNOP"  # built in two parts so this test file is not itself a "leak"


def test_secret_found_without_copying_the_value():
    f = scanners.scan_secrets({"a.py": f'x = 1\nAWS = "{KEY}"\n'})
    assert [(i["path"], i["line"], i["rule"]) for i in f] == [("a.py", 2, "AWS access key")]
    assert KEY not in repr(f)


def test_hardcoded_password_flagged_but_placeholders_and_env_lookups_are_not():
    text = 'password = "hunter2hunter2"\npassword = "your-password-here"\npassword = os.environ["PW"]\napi_key = "${API_KEY}"\n'
    assert [i["line"] for i in scanners.scan_secrets({"c.py": text})] == [1]


def test_private_key_and_docs_skipped():
    assert scanners.scan_secrets({"k.pem": "-----BEGIN RSA PRIVATE KEY-----"})[0]["rule"] == "Private key"
    assert scanners.scan_secrets({"README.md": f"use {KEY}"}) == []


def test_bandit_finds_shell_injection_and_ignores_non_python():
    code = "import subprocess\n\ndef run(cmd):\n    subprocess.call(cmd, shell=True)\n"
    f = scanners.scan_bandit({"x.py": code, "y.js": "eval(foo)"})
    assert f and all(i["path"] == "x.py" and i["tool"] == "bandit" for i in f)
    assert any(i["line"] == 4 and i["severity"] == "high" for i in f)


def test_scan_survives_a_broken_scanner(monkeypatch):
    monkeypatch.setattr(scanners, "scan_bandit", lambda files: 1 / 0)
    assert scanners.scan({"a.py": f'k = "{KEY}"'})[0]["tool"] == "secrets"


def test_findings_text_is_one_line_each_and_capped():
    f = [{"line": n, "tool": "bandit", "rule": "B1", "severity": "low", "message": "a\nb"} for n in range(40)]
    assert len(scanners.findings_text(f).splitlines()) == 25 and "L0 [bandit B1, low] a b" in scanners.findings_text(f)
