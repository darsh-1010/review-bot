import pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src/reviewer"))
import checks  # noqa: E402
import deps  # noqa: E402


def rules(path, text, packs=None):
    return {f["rule"] for f in checks.run_rules({path: text}, packs)}


def test_sql_injection_rules_across_languages():
    assert "SQLI-PY" in rules("a.py", 'cur.execute(f"SELECT * FROM t WHERE id={uid}")')
    assert "SQLI-PY" not in rules("a.py", 'cur.execute("SELECT * FROM t WHERE id=%s", (uid,))')
    assert "SQLI-JS" in rules("a.js", "db.query(`SELECT * FROM t WHERE id=${id}`)")
    assert "SQLI-JS" not in rules("a.js", "db.query('SELECT * FROM t WHERE id=$1', [id])")
    assert "SQLI-PHP" in rules("a.php", 'mysqli_query($c, "SELECT * FROM t WHERE id=" . $_GET["id"]);')
    assert "SQLI-JVM-GO" in rules("A.java", 'stmt.executeQuery("SELECT * FROM t WHERE id=" + id);')


def test_web_pack_only_for_web_and_ai_types():
    xss = 'el.innerHTML = name;'
    assert "XSS" in rules("a.js", xss, checks.PACKS["web"])
    assert "XSS" not in rules("a.js", xss, checks.PACKS["library"])


def test_ai_rules_need_an_llm_file_and_catch_the_big_four():
    llm = ('import anthropic\nc = anthropic.Anthropic()\n'
           'system = f"You help {user_name}"\n'
           'r = c.messages.create(model="m", messages=[])\n'
           'exec(r.content[0].text)\n')
    got = rules("bot.py", llm)
    assert {"AI-PROMPT-INPUT", "AI-NO-MAX-TOKENS", "AI-OUTPUT-EXEC"} <= got
    assert "AI-NO-MAX-TOKENS" not in rules("bot.py", llm.replace('messages=[])', 'messages=[], max_tokens=500)'))
    assert not rules("plain.py", 'system = f"hello {user_name}"\n') & {"AI-PROMPT-INPUT", "AI-NO-MAX-TOKENS"}


def test_infra_rules_are_path_gated():
    assert "GHA-SCRIPT-INJECTION" in rules(".github/workflows/x.yml", "run: echo ${{ github.event.issue.title }}")
    assert "GHA-SCRIPT-INJECTION" not in rules("config/x.yml", "run: echo ${{ github.event.issue.title }}")
    assert "DOCKER-LATEST" in rules("Dockerfile", "FROM python:latest\nRUN echo hi\n")
    assert "IAC-WILDCARD-IAM" in rules("main.tf", 'Action = "*"')


def test_tests_and_examples_are_ignored():
    assert not rules("tests/test_x.py", "eval(x)")


def test_detect_type_and_prompt_extraction():
    assert checks.detect_type({"a.py": "import openai"}) == "ai"
    assert checks.detect_type({"main.tf": "x", "Dockerfile": "FROM a:1"}) == "infra"
    assert checks.detect_type({"app.py": "from flask import Flask"}) == "web"
    assert checks.detect_type({"lib.py": "def f(): pass"}) == "library"
    code = 'import anthropic\nP = """You are a bot. ' + "x" * 200 + '"""\n'
    assert checks.extract_prompts({"b.py": code})[0]["line"] == 2
    secret = [{"path": "b.py", "line": 2, "text": "You are x\npassword: abc12345\n"}]
    assert checks.prompt_secret_findings(secret)[0]["line"] == 3


def test_dependency_parsing_only_trusts_pinned_versions():
    got = deps.parse({"requirements.txt": "flask==2.0.1\nrequests>=2\n# c\nnumpy[x]==1.0\n",
                      "package.json": '{"dependencies": {"left-pad": "^1.3.0", "x": "latest"}}',
                      "package-lock.json": '{"packages": {"": {}, "node_modules/a": {"version": "1.2.3"}}}'})
    names = {(e, n, v) for e, n, v, _ in got}
    assert names == {("PyPI", "flask", "2.0.1"), ("PyPI", "numpy", "1.0"), ("npm", "left-pad", "1.3.0"), ("npm", "a", "1.2.3")}


def test_secret_in_test_file_is_low_and_score_is_capped():
    import scanners, scan
    key = "AKIA" + "A" * 16
    f = scanners.scan_secrets({"tests/test_x.py": f'K="{key}"\n', "app.py": f'K="{key}"\n'})
    assert {x["path"]: x["severity"] for x in f} == {"tests/test_x.py": "low", "app.py": "high"}
    assert scan.score([{"severity": "high"}] * 20, []) == 50
