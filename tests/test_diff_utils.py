import pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src/reviewer"))
import diff_utils as du  # noqa: E402

PATCH = "@@ -1,3 +1,4 @@\n a\n-b\n+c\n+d\n e"


def test_numbering_skips_removed_lines():
    text, valid = du.number_lines(PATCH)
    assert valid == {1, 2, 3, 4}          # a=1, (b removed), c=2, d=3, e=4
    lines = text.splitlines()
    assert lines[1:] == ["L1     a", "      -b", "L2    +c", "L3    +d", "L4     e"]


def test_second_hunk_restarts_numbering():
    _, valid = du.number_lines("@@ -1,1 +1,1 @@\n+x\n@@ -50,2 +60,2 @@\n y\n+z")
    assert valid == {1, 60, 61}


def test_no_newline_marker_ignored():
    _, valid = du.number_lines("@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b")
    assert valid == {1}


def test_skip_reason():
    f = lambda name, **k: {"filename": name, "patch": "@@", **k}
    assert du.skip_reason(f("src/a.py")) is None
    assert du.skip_reason(f("a.py", status="removed"))
    assert du.skip_reason({"filename": "big.bin"})              # no patch
    assert du.skip_reason(f("package-lock.json"))
    assert du.skip_reason(f("poetry.lock"))
    assert du.skip_reason(f("static/app.min.js"))
    assert du.skip_reason(f("logo.PNG"))
    assert du.skip_reason(f("web/dist/x.js"))
    assert du.skip_reason(f("docs/build.md")) is None            # 'build' only as a file name


def test_chunking():
    s = ["a" * 100] * 5
    assert len(du.chunk(s, max_tokens=50)) == 3                  # 200-char budget: 2+2+1
    assert len(du.chunk(s, max_tokens=10_000)) == 1
    big = du.chunk(["x" * 1000], max_tokens=10)
    assert len(big) == 1 and big[0].endswith("[truncated]")
