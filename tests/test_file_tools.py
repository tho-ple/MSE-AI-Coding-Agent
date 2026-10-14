"""File tools: read, search, edit within the copy; reject everything outside the allowed area."""
import pytest


def test_list_files_hides_git(env):
    _, _, _, tools = env
    out = tools.call("list_files", {"path": "."})
    assert not out.denied and not out.error
    assert "shop/cart.py" in out.content
    assert ".git" not in out.content


def test_read_file_numbers_lines_and_ranges(env):
    _, _, _, tools = env
    out = tools.call("read_file", {"path": "shop/discounts.py", "start_line": 5, "end_line": 6})
    assert "6|     \"\"\"Discount in cents" in out.content
    assert "BULK_THRESHOLD = 10" not in out.content  # line 1 was not requested


def test_search_finds_matches(env):
    _, _, _, tools = env
    out = tools.call("search", {"pattern": "bulk_discount"})
    assert "shop/discounts.py:5:" in out.content
    assert "shop/cart.py:" in out.content
    regex = tools.call("search", {"pattern": r"qty\s*>\s*BULK", "regex": True, "path": "shop"})
    assert "shop/discounts.py:7:" in regex.content


def test_edit_file_changes_only_the_intended_file(env):
    ws, _, _, tools = env
    out = tools.call("edit_file", {"path": "shop/discounts.py", "old_text": "qty > BULK_THRESHOLD", "new_text": "qty >= BULK_THRESHOLD"})
    assert not out.error and "+    if qty >= BULK_THRESHOLD:" in out.content
    assert "qty >= BULK_THRESHOLD" in (ws.root / "shop/discounts.py").read_text()
    assert ws.changed_files() == ["M shop/discounts.py"]


def test_edit_requires_unique_match(env):
    ws, _, _, tools = env
    before = (ws.root / "shop/cart.py").read_text()
    out = tools.call("edit_file", {"path": "shop/cart.py", "old_text": "self.lines", "new_text": "x"})
    assert out.error and "found" in out.content and "times" in out.content
    missing = tools.call("edit_file", {"path": "shop/cart.py", "old_text": "does not exist", "new_text": "x"})
    assert missing.error and "not found" in missing.content
    assert (ws.root / "shop/cart.py").read_text() == before


def test_write_file_creates_new_file_in_scope(env):
    ws, _, _, tools = env
    out = tools.call("write_file", {"path": "tests/test_new.py", "content": "def test_x():\n    pass\n"})
    assert not out.denied and (ws.root / "tests/test_new.py").exists()


@pytest.mark.parametrize("path", [
    "../outside.txt",
    "../../etc/passwd",
    "shop/../../escape.py",
    "/etc/passwd",
    "C:/Windows/win.ini",
    "~/secrets",
    ".git/config",
])
def test_rejects_paths_outside_allowed_area(env, path):
    ws, dirs, _, tools = env
    for name, args in [("read_file", {"path": path}),
                       ("write_file", {"path": path, "content": "pwned"}),
                       ("list_files", {"path": path}),
                       ("search", {"pattern": "x", "path": path})]:
        out = tools.call(name, args)
        assert out.denied, (name, path, out.content)
        assert out.content.startswith("DENIED")
    assert not (dirs.root / "outside.txt").exists()


def test_write_outside_writable_scope_is_denied(env):
    ws, _, _, tools = env
    out = tools.call("edit_file", {"path": "README.md", "old_text": "Tiny", "new_text": "Huge"})
    assert out.denied and "writable scope" in out.content
    assert "Tiny" in (ws.root / "README.md").read_text()


def test_checks_dir_is_not_reachable(env):
    _, dirs, _, tools = env
    assert (dirs.checks / "test_acceptance.py").exists()
    out = tools.call("read_file", {"path": "../checks/test_acceptance.py"})
    assert out.denied


def test_symlink_escape_is_rejected(env, tmp_path):
    ws, _, _, tools = env
    secret = tmp_path / "secret.txt"
    secret.write_text("TOKEN=abc")
    link = ws.root / "shop" / "link.txt"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this system")
    out = tools.call("read_file", {"path": "shop/link.txt"})
    assert out.denied and "TOKEN" not in out.content
