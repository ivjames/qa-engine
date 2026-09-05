"""Unit tests for config.py's stdlib .env loader.

config loads <app dir>/.env into os.environ at import time (QA_ENV_FILE
overrides the path); the process environment wins over the file. These tests
point QA_ENV_FILE at a temp file and reload config, restoring the environment
and the module afterwards so the other suites see the state they started with.

Run: .venv/bin/python -m pytest tests/unit/test_config_env.py -q
"""

import importlib
import os

import pytest

import config


@pytest.fixture
def reload_config():
    """Yield a function that reloads config against `env_text` written to a
    temp file, with the given extra process-environment vars; every reload
    restores os.environ and re-imports config on teardown."""
    saved = dict(os.environ)

    def _reload(tmp_path, env_text=None, environ=None, path=None):
        os.environ.clear()
        os.environ.update(saved)
        for key in ("ANTHROPIC_API_KEY", "MOCK_MODELS", "PORT", "QA_ENV_FILE"):
            os.environ.pop(key, None)
        if path is None:
            path = tmp_path / ".env"
            if env_text is not None:
                path.write_text(env_text, encoding="utf-8")
        os.environ["QA_ENV_FILE"] = str(path)
        os.environ.update(environ or {})
        return importlib.reload(config)

    yield _reload
    os.environ.clear()
    os.environ.update(saved)
    importlib.reload(config)


def test_parse_handles_comments_quotes_export_and_crlf():
    text = (
        "# leading comment\r\n"
        "\r\n"
        "PLAIN=value\r\n"
        "export EXPORTED=yes\r\n"
        "  SPACED  =  padded  \n"
        "DQ=\"double quoted # not a comment\"\n"
        "SQ='single quoted'\n"
        "INLINE=abc # trailing comment\n"
        "HASH_NO_SPACE=abc#def\n"
        "EMPTY=\n"
        "EQUALS=a=b=c\n"
        "NOT_EXPANDED=$HOME and `whoami` and ${PLAIN}\n"
        "no-equals-sign\n"
        "1BAD=starts with digit\n"
        "bad key=has space\n"
    )
    parsed = config.parse_env_file(text)
    assert parsed == {
        "PLAIN": "value",
        "EXPORTED": "yes",
        "SPACED": "padded",
        "DQ": "double quoted # not a comment",
        "SQ": "single quoted",
        "INLINE": "abc",
        "HASH_NO_SPACE": "abc#def",
        "EMPTY": "",
        "EQUALS": "a=b=c",
        "NOT_EXPANDED": "$HOME and `whoami` and ${PLAIN}",
    }


def test_temp_env_file_is_read_into_environment(tmp_path, reload_config):
    cfg = reload_config(tmp_path, "ANTHROPIC_API_KEY='sk-ant-test'\nPORT=9999\n")
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test"
    assert cfg.PORT == 9999
    assert cfg.ENV_FILE == str(tmp_path / ".env")
    assert set(cfg.ENV_FILE_LOADED) == {"ANTHROPIC_API_KEY", "PORT"}


def test_process_environment_wins_over_file(tmp_path, reload_config):
    cfg = reload_config(
        tmp_path,
        "PORT=9999\nANTHROPIC_API_KEY=from-file\n",
        environ={"PORT": "8123", "ANTHROPIC_API_KEY": "from-env"},
    )
    assert cfg.PORT == 8123
    assert os.environ["ANTHROPIC_API_KEY"] == "from-env"
    assert cfg.ENV_FILE_LOADED == []


def test_missing_env_file_is_fine(tmp_path, reload_config):
    cfg = reload_config(tmp_path, path=tmp_path / "does-not-exist.env")
    assert cfg.ENV_FILE_LOADED == []
    assert cfg.PORT == 8044
    assert cfg.MOCK_MODELS is True


def test_load_env_file_into_explicit_mapping_does_not_touch_os_environ(tmp_path):
    path = tmp_path / "x.env"
    path.write_text("ONLY_IN_MAPPING=1\nKEEP=file\n")
    target = {"KEEP": "already"}
    assert config.load_env_file(str(path), target) == ["ONLY_IN_MAPPING"]
    assert target == {"KEEP": "already", "ONLY_IN_MAPPING": "1"}
    assert "ONLY_IN_MAPPING" not in os.environ


def test_mock_models_flips_off_when_file_supplies_key(tmp_path, reload_config):
    cfg = reload_config(tmp_path, "ANTHROPIC_API_KEY=sk-ant-test\n")
    assert cfg.MOCK_MODELS is False


def test_mock_models_stays_on_when_forced_despite_key_in_file(tmp_path, reload_config):
    cfg = reload_config(tmp_path, "ANTHROPIC_API_KEY=sk-ant-test\nMOCK_MODELS=1\n")
    assert cfg.MOCK_MODELS is True


def test_mock_models_on_when_file_has_blank_key(tmp_path, reload_config):
    cfg = reload_config(tmp_path, "ANTHROPIC_API_KEY=\n")
    assert cfg.MOCK_MODELS is True
