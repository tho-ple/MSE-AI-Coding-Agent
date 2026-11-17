"""Navigation: modules, symbols, dependencies and Git state needed for a task."""
from pathlib import Path

import pytest

from coding_harness.navigation import RepoIndex, module_name

ROOT = Path(__file__).resolve().parents[1]


def test_find_symbol_definitions(env):
    _, _, _, tools = env
    out = tools.call("find_symbol", {"name": "bulk_discount"})
    assert not out.error and "shop/discounts.py:5: function bulk_discount(qty: int, line_total: int)" in out.content
    out = tools.call("find_symbol", {"name": "Cart.total"})
    assert "shop/cart.py" in out.content and "method Cart.total" in out.content
    assert "No definition" in tools.call("find_symbol", {"name": "does_not_exist"}).content


def test_find_references_shows_existing_callers(env):
    _, _, _, tools = env
    out = tools.call("find_references", {"name": "price_of"})
    lines = out.content.splitlines()
    assert any(line.startswith("shop/catalog.py:") and "[definition]" in line for line in lines)
    assert sum("shop/cart.py" in line and "[call]" in line for line in lines) == 2
    tests_only = tools.call("find_references", {"name": "Cart", "path": "tests"}).content
    assert "tests/test_cart.py" in tests_only and "shop/" not in tests_only


def test_module_dependencies_both_directions(env):
    _, _, _, tools = env
    cart = tools.call("module_deps", {"path": "shop/cart.py"}).content
    assert "imports: shop/catalog.py, shop/discounts.py" in cart
    assert "imported by: tests/test_cart.py" in cart
    disc = tools.call("module_deps", {"path": "shop/discounts.py"}).content
    assert "imported by: shop/cart.py" in disc
    assert tools.call("module_deps", {"path": "README.md"}).error


def test_repo_map_is_compact_and_bounded(env, settings):
    _, _, _, tools = env
    out = tools.call("repo_map", {"path": "."}).content
    assert "shop/cart.py: class Cart(__init__, add, total)" in out
    assert "shop/discounts.py: BULK_THRESHOLD; BULK_PERCENT; def bulk_discount" in out
    index = tools.index
    small = index.repo_map(200)
    assert len(small) <= 200 and "more modules not shown" in small


def test_relative_imports_and_src_layout(tmp_path):
    pkg = tmp_path / "src" / "app"
    (pkg / "domain").mkdir(parents=True)
    (tmp_path / "src" / "setup.py").write_text("")
    for d in (pkg, pkg / "domain"):
        (d / "__init__.py").write_text("")
    (pkg / "domain" / "model.py").write_text("from . import events\nfrom ..util import helper\n\nclass Order:\n    pass\n")
    (pkg / "domain" / "events.py").write_text("class Placed:\n    pass\n")
    (pkg / "util.py").write_text("def helper():\n    return 1\n")
    assert module_name("src/app/domain/model.py", tmp_path) == "app.domain.model"
    from coding_harness.workspace import Workspace
    index = RepoIndex(Workspace(tmp_path, ["**"], []))
    _, deps, _ = index.module_deps("src/app/domain/model.py")
    assert deps == ["src/app/domain/events.py", "src/app/util.py"]


def test_index_follows_edits(env):
    ws, _, _, tools = env
    assert "No definition" in tools.call("find_symbol", {"name": "coupon_percent"}).content
    tools.call("edit_file", {"path": "shop/discounts.py", "old_text": "BULK_PERCENT = 10\n",
                             "new_text": "BULK_PERCENT = 10\n\n\ndef coupon_percent(code):\n    return 0\n"})
    assert "function coupon_percent(code)" in tools.call("find_symbol", {"name": "coupon_percent"}).content


def test_git_state_shows_baseline_and_changes(env, settings):
    ws, _, _, tools = env
    before = tools.call("git_status", {}).content
    assert "Workspace baseline commit:" in before and "baseline" in before
    assert "Changed files vs. baseline: 0" in before and "Remotes: none" in before
    tools.call("edit_file", {"path": "shop/discounts.py", "old_text": "if qty > BULK_THRESHOLD:",
                             "new_text": "if qty >= BULK_THRESHOLD:"})
    tools.call("write_file", {"path": "tests/test_new.py", "content": "def test_x():\n    pass\n"})
    after = tools.call("git_status", {}).content
    assert "Changed files vs. baseline: 2" in after
    assert "  M shop/discounts.py" in after and "  A tests/test_new.py" in after
    diff = tools.call("git_diff", {"path": "shop/discounts.py"}).content
    assert "+    if qty >= BULK_THRESHOLD:" in diff and "test_new" not in diff


def test_navigation_tools_reject_paths_outside_copy(env):
    _, _, _, tools = env
    for name, args in [("module_deps", {"path": "../x.py"}), ("repo_map", {"path": "/etc"}),
                       ("find_references", {"name": "x", "path": "../.."})]:
        assert tools.call(name, args).denied


@pytest.mark.skipif(not (ROOT / "targets" / "cosmicpython" / "src").exists(), reason="Cosmic Python target missing")
def test_cosmic_feature_modules_are_found(tmp_path):
    """The modules the Deallocate feature needs: commands, domain model, handlers (+ message bus wiring)."""
    from coding_harness.config import load_settings
    from coding_harness.workspace import Workspace
    s = load_settings(ROOT / "examples" / "cosmic-feature" / "harness.yaml")
    s.runs_dir = tmp_path
    ws, _ = Workspace.create(s, "nav")
    index = RepoIndex(ws)
    task = (ROOT / "examples" / "cosmic-feature" / "task.md").read_text()
    picked = [p for p, _, _ in index.select_files(task, 6)]
    for needed in ("src/allocation/domain/commands.py", "src/allocation/domain/model.py",
                   "src/allocation/service_layer/handlers.py"):
        assert needed in picked, picked
    _, deps, rdeps = index.module_deps("src/allocation/service_layer/handlers.py")
    assert "src/allocation/domain/model.py" in deps and "src/allocation/domain/commands.py" in deps
    assert "src/allocation/bootstrap.py" in rdeps
    assert any(s.qualname == "Product.change_batch_quantity" for s in index.find_symbol("change_batch_quantity"))
