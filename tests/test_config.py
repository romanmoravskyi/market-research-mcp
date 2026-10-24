"""Where `.env` is loaded from: MRM_ENV_FILE, else the per-user config file, then cwd.

Never from the plugin root (`${CLAUDE_PLUGIN_ROOT}` is replaced on every plugin update).
"""

from pathlib import Path

import pytest

from market_research_mcp import config

ISOLATED = (*config.ENV_VARS, "MRM_ENV_FILE", "CLAUDE_PLUGIN_ROOT", "APPDATA", "XDG_CONFIG_HOME")


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Clean env: every var used here is restored (or removed) after the test."""
    for name in ISOLATED:
        monkeypatch.setenv(name, "")  # records the original value for undo
        monkeypatch.delenv(name)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_user_config_dir_windows_and_posix(env):
    assert config.user_config_dir(windows=True) == env / "appdata" / "market-research-mcp"
    assert config.user_config_dir(windows=False) == env / "xdg" / "market-research-mcp"


def test_user_config_dir_posix_default(env, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME")
    expected = Path.home() / ".config" / "market-research-mcp"
    assert config.user_config_dir(windows=False) == expected


def test_mrm_env_file_wins_and_skips_user_config(env, monkeypatch):
    explicit = _write(env / "custom.env", "FRED_API_KEY=from-explicit\n")
    _write(config.user_config_dir() / ".env", "FRED_API_KEY=from-user\nTIINGO_API_KEY=user\n")
    monkeypatch.setenv("MRM_ENV_FILE", str(explicit))
    settings = config.load_settings()
    assert settings.fred_api_key == "from-explicit"
    assert settings.tiingo_api_key is None


def test_user_config_then_cwd(env):
    _write(config.user_config_dir() / ".env", "FRED_API_KEY=from-user\n")
    _write(env / "work" / ".env", "FRED_API_KEY=from-cwd\nTIINGO_API_KEY=from-cwd\n")
    settings = config.load_settings()
    assert settings.fred_api_key == "from-user"
    assert settings.tiingo_api_key == "from-cwd"


def test_real_env_beats_files(env, monkeypatch):
    _write(env / "work" / ".env", "FRED_API_KEY=from-cwd\n")
    monkeypatch.setenv("FRED_API_KEY", "from-env")
    assert config.load_settings().fred_api_key == "from-env"


def test_never_loads_from_plugin_root(env, monkeypatch):
    _write(env / "work" / ".env", "FRED_API_KEY=from-plugin-root\n")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(env / "work"))
    assert config.load_settings().fred_api_key is None


def test_does_not_walk_up_from_package_dir(env):
    assert all(
        p.parent != Path(config.__file__).resolve().parents[2] for p in config.dotenv_paths()
    )


def test_journal_dir_from_settings_and_cli(env, capsys):
    _write(config.user_config_dir() / ".env", f"JOURNAL_DIR={env / 'journal'}\n")
    assert config.load_settings().journal_dir == env / "journal"
    assert config.main(["journal-dir"]) == 0
    assert capsys.readouterr().out.strip() == str(env / "journal")


def test_journal_dir_cli_prints_nothing_when_unset(env, capsys):
    assert config.main(["journal-dir"]) == 0
    assert capsys.readouterr().out.strip() == ""
    assert config.main([]) == 2
