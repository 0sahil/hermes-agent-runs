"""Release hygiene: versions agree, settings match, the desktop half stays inside the SDK rules, and
every backend module loads by file path the way Hermes loads it (no package, no sys.modules entry)."""
import ast
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_JS = (ROOT / "desktop" / "plugin.js").read_text("utf-8")


def plugin_yaml():
    return (ROOT / "plugin.yaml").read_text("utf-8")


def test_versions_agree():
    import runs_core
    version = re.search(r"^version:\s*(\S+)$", plugin_yaml(), re.M).group(1)
    manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text("utf-8"))
    assert version == manifest["version"] == runs_core.__version__ == "0.1.0"
    assert manifest["name"] == runs_core.PLUGIN_ID == "agent-runs"
    assert re.search(r"^name:\s*agent-runs$", plugin_yaml(), re.M)
    assert f"const ID = '{runs_core.PLUGIN_ID}'" in PLUGIN_JS


def test_config_schema_matches_the_core_defaults():
    import runs_core
    schema = plugin_yaml().split("config_schema:", 1)[1]
    keys = re.findall(r"^  ([a-z_]+):\s*$", schema, re.M)
    assert keys == list(runs_core.DEFAULT_SETTINGS)
    for key, value in runs_core.DEFAULT_SETTINGS.items():
        block = schema.split(f"\n  {key}:", 1)[1]  # the first `default:` after the key is its own
        default = re.search(r"default:\s*(\S+)", block).group(1)
        assert default == (str(value).lower() if isinstance(value, bool) else str(value)), key


@pytest.mark.parametrize("rel", ["procs.py", "runs_core.py", "adapters/claude.py"])
def test_backend_modules_load_by_path_without_a_package(rel):
    path = ROOT / "dashboard" / rel
    spec = importlib.util.spec_from_file_location(f"loaded_by_path_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # deliberately not registered in sys.modules, like the plugin host
    assert module.__doc__


def test_every_python_module_has_a_docstring():
    for path in [ROOT / "__init__.py", *sorted((ROOT / "dashboard").rglob("*.py"))]:
        assert ast.get_docstring(ast.parse(path.read_text("utf-8"))), path


def test_core_imports_no_agent_specifics_or_frameworks():
    tree = ast.parse((ROOT / "dashboard" / "runs_core.py").read_text("utf-8"))
    imported = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & {"psutil", "fastapi", "adapters", "claude", "procs"}


# --- desktop/plugin.js -------------------------------------------------------------------------


def js_imports():
    names = set()
    for spec_list, source in re.findall(r"import\s*\{([^}]*)\}\s*from\s*'([^']+)'", PLUGIN_JS):
        names |= {n.strip().split(" as ")[-1] for n in spec_list.split(",") if n.strip()}
    return names


def test_plugin_js_imports_only_the_sdk_and_react():
    specifiers = set(re.findall(r"^import\b[^;]*?\bfrom\s*'([^']+)'", PLUGIN_JS, re.M | re.S))
    assert specifiers == {"@hermes/plugin-sdk", "react", "react/jsx-runtime"}
    assert "import(" not in PLUGIN_JS


def test_every_component_used_in_jsx_is_imported_or_defined():
    used = set(re.findall(r"\bjsxs?\(\s*([A-Z][A-Za-z0-9]*)\b", PLUGIN_JS))
    defined = set(re.findall(r"^(?:function|class)\s+([A-Z][A-Za-z0-9]*)", PLUGIN_JS, re.M))
    missing = used - js_imports() - defined
    assert not missing, f"used in jsx() but never imported: {sorted(missing)}"


def test_plugin_js_has_no_eval_markup_reaching_or_hardcoded_colours():
    for pattern in (r"\beval\s*\(", r"new\s+Function", r"<script", r"document\.querySelector", r"data-slot",
                    r"window\.hermesDesktop", r"#[0-9a-fA-F]{3,8}\b", r"\brgba?\(", r"\bhsla?\(",
                    r"'(?:black|white|red|green|blue|gray|grey)'"):
        assert not re.search(pattern, PLUGIN_JS), pattern


def test_plugin_js_registers_pane_chip_and_command():
    assert "area: 'panes'" in PLUGIN_JS and "placement: 'right'" in PLUGIN_JS
    assert "area: 'statusBar.right'" in PLUGIN_JS
    assert "area: PALETTE_AREA" in PLUGIN_JS and "label: 'Show agent runs'" in PLUGIN_JS


def test_no_macos_home_paths_in_shipped_files():
    needle = "/" + "Users" + "/"  # spelled out so this file does not match itself
    skip = {".git", "drafts", "__pycache__", ".pytest_cache"}
    for path in ROOT.rglob("*"):
        parts = path.relative_to(ROOT).parts
        if not path.is_file() or skip & set(parts) or parts[0].startswith(".venv"):
            continue
        try:
            text = path.read_text("utf-8")
        except UnicodeDecodeError:
            continue
        assert needle not in text, path
