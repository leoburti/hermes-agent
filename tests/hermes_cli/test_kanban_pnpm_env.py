import shutil
import subprocess

import pytest

from hermes_cli import kanban_pnpm_env as m


def test_defaults_applied():
    env = {"HOME": "/Users/x"}
    m.apply_pnpm_store_env(env, {"pnpm_store_dir": "/s/store"})
    assert env["npm_config_store_dir"] == "/s/store"
    assert env["npm_config_package_import_method"] == "hardlink"
    assert env["npm_config_prefer_offline"] == "true"
    assert "npm_config_enable_global_virtual_store" not in env


def test_operator_env_wins_and_disable():
    env = {"NPM_CONFIG_STORE_DIR": "/mine", "npm_config_package_import_method": "copy"}
    m.apply_pnpm_store_env(env, {})
    assert "npm_config_store_dir" not in env and env["npm_config_package_import_method"] == "copy"
    env2 = {}
    m.apply_pnpm_store_env(env2, {"pnpm_shared_store": False})
    assert env2 == {}


def test_invalid_method_falls_back_and_gvs_opt_in():
    env = {}
    m.apply_pnpm_store_env(env, {"pnpm_package_import_method": "symlink-yolo", "pnpm_global_virtual_store": True})
    assert env["npm_config_package_import_method"] == "hardlink"
    assert env["npm_config_enable_global_virtual_store"] == "true"


def test_default_store_dir_is_platform_default(monkeypatch):
    monkeypatch.delenv("PNPM_HOME", raising=False)
    monkeypatch.setattr(m.sys, "platform", "darwin")
    assert m.default_store_dir("/Users/x") == "/Users/x/Library/pnpm/store"


@pytest.mark.skipif(shutil.which("pnpm") is None, reason="pnpm not installed")
def test_pnpm_honours_env(tmp_path):
    env = m.apply_pnpm_store_env({"PATH": __import__("os").environ["PATH"], "HOME": str(tmp_path)},
                                 {"pnpm_store_dir": str(tmp_path / "store")})
    out = subprocess.run(["pnpm", "config", "get", "package-import-method"], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=60, check=True)
    assert out.stdout.strip() == "hardlink"
