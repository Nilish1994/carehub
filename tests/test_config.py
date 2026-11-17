"""Effective-configuration tests for docker-compose.yml, .env.example and tools/check_config.py.

The Compose emulation follows Docker's documented precedence: a value set in
the service's `environment` beats `env_file`, and `${VAR:-default}` is
interpolated from the project .env. When Docker is installed the same checks
also run through `docker compose config` (skipped otherwise).
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
import yaml

import check_config as cc
from conftest import ROOT

COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
EXAMPLE = (ROOT / ".env.example").read_text()
FILLED = {
    "PAPERLESS_SECRET_KEY": "test-secret-key-not-real",
    "PAPERLESS_ADMIN_USER": "admin",
    "PAPERLESS_ADMIN_PASSWORD": "test-admin-password",
    "PAPERLESS_AI_LLM_API_KEY": "test-api-key",
    "DEMO_MANAGER_PASSWORD": "m", "DEMO_ADMIN_PASSWORD": "a", "DEMO_CARER_PASSWORD": "c",
}

# The webserver `environment` block as it was at c553433 (literal embedding settings).
OLD_COMPOSE = yaml.safe_load("""
services:
  webserver:
    env_file: .env
    ports: ["127.0.0.1:${CAREHUB_PORT:-8040}:8000"]
    environment:
      PAPERLESS_AI_ENABLED: "true"
      PAPERLESS_AI_LLM_EMBEDDING_BACKEND: huggingface
      PAPERLESS_AI_LLM_EMBEDDING_MODEL: sentence-transformers/all-MiniLM-L6-v2
""")


def fill(text: str, values: dict[str, str]) -> str:
    out = []
    for line in text.splitlines():
        key = line.split("=", 1)[0]
        out.append(f"{key}={values[key]}" if key in values and "=" in line and not line.startswith("#") else line)
    return "\n".join(out) + "\n"


def profile_b(text: str) -> str:
    """Comment out the Option A block and enable the Option B block, as a user would."""
    out, section = [], None
    for line in text.splitlines():
        if line.startswith("# Option A"):
            section = "A"
        elif line.startswith("# Option B"):
            section = "B"
        elif line.startswith("# Set to 1 once"):
            section = None
        if section == "A" and line and not line.startswith("#"):
            line = "# " + line
        elif section == "B" and line.startswith("# PAPERLESS_"):
            line = line[2:]
        out.append(line)
    return "\n".join(out) + "\n"


def effective(env_text: str, compose: dict = COMPOSE) -> tuple[cc.EnvFile, dict[str, str]]:
    env_file = cc.parse_env(env_text)
    return env_file, cc.effective_environment(compose, env_file.values, ROOT)


def test_example_parses_and_keeps_quoted_json():
    env = cc.parse_env(EXAMPLE)
    assert env.duplicates == []
    assert json.loads(env.values["PAPERLESS_AI_LLM_EXTRA_PARAMS"]) == {"temperature": None}
    for name in ("DEMO_MANAGER_PASSWORD", "DEMO_ADMIN_PASSWORD", "DEMO_CARER_PASSWORD", "CAREHUB_PORT"):
        assert name in env.values


def test_profile_a_effective_config():
    env_file, eff = effective(fill(EXAMPLE, FILLED))
    profile = cc.ai_profile(eff)
    assert profile["llm_backend"] == "openai-like"
    assert profile["llm_endpoint"] == "https://api.anthropic.com/v1/"
    assert profile["embedding_backend"] == "huggingface"
    assert profile["embedding_model"] == "sentence-transformers/all-MiniLM-L6-v2"
    assert profile["embedding_endpoint"] is None
    assert profile["index_enabled"] is True
    errors, warnings = cc.validate(env_file, eff, COMPOSE)
    assert errors == []
    assert any("api.anthropic.com" in w for w in warnings)


def test_profile_b_effective_config_uses_ollama_embeddings():
    env_file, eff = effective(fill(profile_b(EXAMPLE), FILLED))
    profile = cc.ai_profile(eff)
    assert profile["llm_backend"] == "ollama"
    assert profile["embedding_backend"] == "ollama"
    assert profile["embedding_model"] == "embeddinggemma"
    assert profile["embedding_endpoint"] == "http://host.docker.internal:11434"
    errors, warnings = cc.validate(env_file, eff, COMPOSE)
    assert errors == []
    assert not any("sends" in w or "send document text" in w for w in warnings)
    assert not any("extra_hosts" in w for w in warnings)


def test_old_compose_overrode_ollama_embeddings():
    """Regression: the literal environment entries beat the .env choice."""
    _, eff = effective(fill(profile_b(EXAMPLE), FILLED), OLD_COMPOSE)
    assert cc.ai_profile(eff)["embedding_backend"] == "huggingface"


def test_embedding_defaults_apply_when_env_leaves_them_unset():
    text = "\n".join(line for line in fill(EXAMPLE, FILLED).splitlines()
                     if not line.startswith("PAPERLESS_AI_LLM_EMBEDDING"))
    _, eff = effective(text)
    assert eff["PAPERLESS_AI_LLM_EMBEDDING_BACKEND"] == "huggingface"
    assert eff["PAPERLESS_AI_LLM_EMBEDDING_MODEL"] == "sentence-transformers/all-MiniLM-L6-v2"


def test_enabling_both_options_is_flagged():
    text = fill(EXAMPLE, FILLED) + "PAPERLESS_AI_LLM_EMBEDDING_BACKEND=ollama\n"
    env_file, eff = effective(text)
    _, warnings = cc.validate(env_file, eff, COMPOSE)
    assert any("more than once" in w for w in warnings)


def test_remote_embeddings_from_local_llm_are_flagged():
    text = fill(profile_b(EXAMPLE), FILLED).replace("EMBEDDING_BACKEND=ollama", "EMBEDDING_BACKEND=openai-like")
    text += "PAPERLESS_AI_LLM_EMBEDDING_ENDPOINT=https://api.openai.com/v1/\n"
    env_file, eff = effective(text)
    _, warnings = cc.validate(env_file, eff, COMPOSE)
    assert any("embeddings send document text to api.openai.com" in w for w in warnings)
    assert any("not computed locally" in w for w in warnings)


def test_host_docker_internal_without_mapping_is_flagged():
    compose = yaml.safe_load(yaml.safe_dump(COMPOSE))
    compose["services"]["webserver"].pop("extra_hosts")
    env_file, eff = effective(fill(profile_b(EXAMPLE), FILLED), compose)
    _, warnings = cc.validate(env_file, eff, compose)
    assert any("extra_hosts" in w for w in warnings)


@pytest.mark.parametrize("override,message", [
    ({"PAPERLESS_SECRET_KEY": ""}, "PAPERLESS_SECRET_KEY"),
    ({"PAPERLESS_SECRET_KEY": "change-me"}, "PAPERLESS_SECRET_KEY"),
    ({"PAPERLESS_AI_LLM_API_KEY": ""}, "PAPERLESS_AI_LLM_API_KEY"),
    ({"CAREHUB_PORT": "eighty"}, "CAREHUB_PORT"),
    ({"PAPERLESS_AI_LLM_EXTRA_PARAMS": "[1]"}, "JSON object"),
    ({"PAPERLESS_AI_LLM_BACKEND": "anthropic"}, "PAPERLESS_AI_LLM_BACKEND"),
])
def test_invalid_settings_are_errors(override, message):
    env_file, eff = effective(fill(EXAMPLE, {**FILLED, **override}))
    errors, _ = cc.validate(env_file, eff, COMPOSE)
    assert any(message in e for e in errors), errors


def test_missing_demo_passwords_are_warned():
    env_file, eff = effective(EXAMPLE)
    _, warnings = cc.validate(env_file, eff, COMPOSE)
    assert any("DEMO_CARER_PASSWORD" in w for w in warnings)


def test_non_default_port_is_published():
    env = cc.parse_env(fill(EXAMPLE, {**FILLED, "CAREHUB_PORT": "8123"})).values
    assert cc.published_ports(COMPOSE, env) == ["127.0.0.1:8123:8000"]
    assert cc.host_port(COMPOSE, env) == 8123


def test_nginx_port_mismatch_is_an_error():
    nginx = (ROOT / "deploy" / "nginx-carehub.conf").read_text()
    assert cc.nginx_upstream_ports(nginx) == [8040]
    env_file, eff = effective(fill(EXAMPLE, {**FILLED, "CAREHUB_PORT": "8123"}))
    errors, _ = cc.validate(env_file, eff, COMPOSE, nginx)
    assert any("do not match the published CareHub port 8123" in e for e in errors)
    env_file, eff = effective(fill(EXAMPLE, FILLED))
    errors, _ = cc.validate(env_file, eff, COMPOSE, nginx)
    assert errors == []


def test_cli_masks_secrets(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text(fill(EXAMPLE, FILLED))
    code = cc.main(["--env-file", str(env), "--compose", str(ROOT / "docker-compose.yml"),
                    "--nginx", str(ROOT / "deploy" / "nginx-carehub.conf")])
    out = capsys.readouterr().out
    assert code == 0
    for secret in FILLED.values():
        if len(secret) > 3:
            assert secret not in out
    assert "embedding_backend: huggingface" in out


def test_images_are_pinned_by_digest():
    for name, svc in COMPOSE["services"].items():
        assert "@sha256:" in svc["image"], name


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker not installed")
@pytest.mark.parametrize("profile", ["A", "B"])
def test_docker_compose_config_matches_emulation(tmp_path, profile):
    text = fill(EXAMPLE if profile == "A" else profile_b(EXAMPLE), {**FILLED, "CAREHUB_PORT": "8123"})
    shutil.copy(ROOT / "docker-compose.yml", tmp_path / "docker-compose.yml")
    (tmp_path / ".env").write_text(text)
    result = subprocess.run(["docker", "compose", "--project-directory", str(tmp_path), "-f",
                             str(tmp_path / "docker-compose.yml"), "config", "--format", "json"],
                            capture_output=True, text=True, check=True)
    rendered = json.loads(result.stdout)["services"]["webserver"]
    expected = cc.effective_environment(COMPOSE, cc.parse_env(text).values, ROOT)
    for key in ("PAPERLESS_AI_LLM_BACKEND", "PAPERLESS_AI_LLM_EMBEDDING_BACKEND", "PAPERLESS_AI_LLM_EMBEDDING_MODEL",
                "PAPERLESS_AI_LLM_ENDPOINT"):
        assert rendered["environment"].get(key) == expected.get(key), key
    assert rendered["ports"][0]["published"] == "8123"
