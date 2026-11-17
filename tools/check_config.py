"""Check a CareHub .env file and show the effective AI configuration.

Reads docker-compose.yml and an env file (default .env), applies Docker
Compose's documented precedence for the webserver service (an `environment`
entry beats `env_file`; `${VAR:-default}` is interpolated from the env file),
and reports:

* errors: settings that stop Paperless-ngx or the demo seeder from working;
* warnings: settings that change where document text goes or need care;
* the effective AI profile (LLM and embedding backends, models, endpoints).

Secret values are never printed. This emulates Compose; when Docker is
available, `docker compose config` is the authoritative rendering.

Also note: values saved on Paperless-ngx's Configuration page (stored in its
database) override these environment variables for AI settings.

Usage: python tools/check_config.py [--env-file .env] [--compose docker-compose.yml]
                                    [--nginx deploy/nginx-carehub.conf]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

SERVICE = "webserver"
LLM_BACKENDS = {"ollama", "openai-like"}
EMBEDDING_BACKENDS = {"huggingface", "openai-like", "ollama"}
# Defaults applied by Paperless-ngx 3.4.2 (src/paperless_ai/embedding.py, client.py)
DEFAULT_EMBEDDING_MODEL = {
    "huggingface": "sentence-transformers/all-MiniLM-L6-v2",
    "ollama": "embeddinggemma",
    "openai-like": "text-embedding-3-small",
}
OLLAMA_DEFAULT_ENDPOINT = "http://localhost:11434"
DEMO_PASSWORD_VARS = ("DEMO_MANAGER_PASSWORD", "DEMO_ADMIN_PASSWORD", "DEMO_CARER_PASSWORD")
SECRET_HINTS = ("PASSWORD", "SECRET", "API_KEY", "TOKEN")

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_VAR = re.compile(r"\$(?:\$|\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?+])([^}]*))?\}|([A-Za-z_][A-Za-z0-9_]*))")


class EnvParseError(ValueError):
    pass


@dataclass
class EnvFile:
    values: dict[str, str]
    duplicates: list[str] = field(default_factory=list)


def parse_env(text: str) -> EnvFile:
    """Parse a dotenv file the way Compose does for the cases used here."""
    values: dict[str, str] = {}
    duplicates: list[str] = []
    for number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _LINE.match(raw)
        if not match:
            raise EnvParseError(f"line {number}: expected KEY=VALUE")
        key, value = match.group(1), match.group(2).strip()
        if value[:1] in ("'", '"'):
            quote = value[0]
            end = value.find(quote, 1)
            if end == -1:
                raise EnvParseError(f"line {number}: unterminated quote for {key}")
            value = value[1:end]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        if key in values:
            duplicates.append(key)
        values[key] = value
    return EnvFile(values, duplicates)


def interpolate(value: str, env: dict[str, str]) -> str:
    def replace(m: re.Match) -> str:
        if m.group(0) == "$$":
            return "$"
        name = m.group(1) or m.group(4)
        op, arg = m.group(2), m.group(3) or ""
        current = env.get(name)
        if op == ":-":
            return current if current else arg
        if op == "-":
            return current if current is not None else arg
        if op in (":?", "?"):
            unset = (not current) if op == ":?" else (current is None)
            if unset:
                raise EnvParseError(f"required variable {name} is not set ({arg})")
            return current
        if op in (":+", "+"):
            present = bool(current) if op == ":+" else (current is not None)
            return arg if present else ""
        return current or ""
    return _VAR.sub(replace, value)


def _env_entries(environment: Any) -> list[tuple[str, str]]:
    if isinstance(environment, dict):
        return [(k, "" if v is None else str(v)) for k, v in environment.items()]
    entries = []
    for item in environment or []:
        key, _, value = str(item).partition("=")
        entries.append((key, value))
    return entries


def effective_environment(compose: dict, env: dict[str, str], project_dir: Path,
                          service: str = SERVICE) -> dict[str, str]:
    """Container environment of `service`: env_file entries, then `environment` on top."""
    svc = compose["services"][service]
    result: dict[str, str] = {}
    env_files = svc.get("env_file") or []
    if isinstance(env_files, (str, dict)):
        env_files = [env_files]
    for entry in env_files:
        path = entry["path"] if isinstance(entry, dict) else entry
        if Path(path).name == ".env" and (project_dir / path).resolve() == (project_dir / ".env").resolve():
            result.update(env)
        else:
            result.update(parse_env((project_dir / path).read_text()).values)
    for key, value in _env_entries(svc.get("environment")):
        result[key] = interpolate(value, env)
    return result


def published_ports(compose: dict, env: dict[str, str], service: str = SERVICE) -> list[str]:
    return [interpolate(str(p), env) for p in compose["services"][service].get("ports") or []]


def host_port(compose: dict, env: dict[str, str], container_port: int = 8000) -> int | None:
    for mapping in published_ports(compose, env):
        parts = mapping.split(":")
        if parts and parts[-1] == str(container_port) and len(parts) >= 2 and parts[-2].isdigit():
            return int(parts[-2])
    return None


def ai_profile(effective: dict[str, str]) -> dict[str, Any]:
    enabled = effective.get("PAPERLESS_AI_ENABLED", "").lower() in ("1", "true", "yes", "y", "t", "on")
    llm = effective.get("PAPERLESS_AI_LLM_BACKEND") or None
    emb = effective.get("PAPERLESS_AI_LLM_EMBEDDING_BACKEND") or None
    llm_endpoint = effective.get("PAPERLESS_AI_LLM_ENDPOINT") or None
    emb_endpoint = effective.get("PAPERLESS_AI_LLM_EMBEDDING_ENDPOINT") or None
    if emb == "ollama":
        emb_endpoint = emb_endpoint or llm_endpoint or OLLAMA_DEFAULT_ENDPOINT
    elif emb == "openai-like":
        emb_endpoint = emb_endpoint or llm_endpoint
    elif emb == "huggingface":
        emb_endpoint = None  # computed in the container; model fetched from Hugging Face unless cached
    if llm == "ollama":
        llm_endpoint = llm_endpoint or OLLAMA_DEFAULT_ENDPOINT
    return {
        "ai_enabled": enabled,
        "llm_backend": llm,
        "llm_model": effective.get("PAPERLESS_AI_LLM_MODEL") or None,
        "llm_endpoint": llm_endpoint,
        "embedding_backend": emb,
        "embedding_model": (effective.get("PAPERLESS_AI_LLM_EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL.get(emb or "")
                            or None),
        "embedding_endpoint": emb_endpoint,
        "index_enabled": bool(enabled and emb),
        "hf_hub_offline": effective.get("HF_HUB_OFFLINE", ""),
    }


def _is_local_host(url: str | None) -> bool:
    if not url:
        return True
    host = (urlsplit(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1", "host.docker.internal") or host.endswith(".local") or \
        re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", host) is not None


def nginx_upstream_ports(text: str) -> list[int]:
    return [int(p) for p in re.findall(r"proxy_pass\s+https?://(?:127\.0\.0\.1|localhost|\[::1\]):(\d+)", text)]


def validate(env_file: EnvFile, effective: dict[str, str], compose: dict,
             nginx_text: str | None = None) -> tuple[list[str], list[str]]:
    env = env_file.values
    errors: list[str] = []
    warnings: list[str] = []
    for key in env_file.duplicates:
        warnings.append(f"{key} is set more than once; the last value wins (did you enable both AI options?)")

    secret = env.get("PAPERLESS_SECRET_KEY", "").strip()
    if not secret or secret == "change-me":
        errors.append("PAPERLESS_SECRET_KEY is empty or 'change-me'; Paperless-ngx 3.4.2 refuses to start")
    for key in ("PAPERLESS_ADMIN_USER", "PAPERLESS_ADMIN_PASSWORD"):
        if not env.get(key):
            errors.append(f"{key} is empty")
    port = env.get("CAREHUB_PORT", "8040") or "8040"
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        errors.append(f"CAREHUB_PORT must be a port number, got {port!r}")

    profile = ai_profile(effective)
    llm, emb = profile["llm_backend"], profile["embedding_backend"]
    if llm is not None and llm not in LLM_BACKENDS:
        errors.append(f"PAPERLESS_AI_LLM_BACKEND={llm!r} is not one of {sorted(LLM_BACKENDS)}")
    if emb is not None and emb not in EMBEDDING_BACKENDS:
        errors.append(f"PAPERLESS_AI_LLM_EMBEDDING_BACKEND={emb!r} is not one of {sorted(EMBEDDING_BACKENDS)}")
    extra = effective.get("PAPERLESS_AI_LLM_EXTRA_PARAMS")
    if extra:
        try:
            if not isinstance(json.loads(extra), dict):
                raise ValueError
        except ValueError:
            errors.append("PAPERLESS_AI_LLM_EXTRA_PARAMS must be a JSON object")
    if profile["ai_enabled"]:
        if llm == "openai-like" and not effective.get("PAPERLESS_AI_LLM_API_KEY"):
            errors.append("PAPERLESS_AI_LLM_API_KEY is empty but the LLM backend is a hosted openai-like endpoint")
        if llm == "openai-like" and not _is_local_host(profile["llm_endpoint"]):
            warnings.append(f"chat sends question-relevant document text to {urlsplit(profile['llm_endpoint']).hostname}")
        if emb == "openai-like" and not _is_local_host(profile["embedding_endpoint"]):
            warnings.append("embeddings send document text to "
                            f"{urlsplit(profile['embedding_endpoint'] or '').hostname or 'the OpenAI default endpoint'}")
        if llm == "ollama" and emb not in ("ollama", "huggingface"):
            warnings.append("LLM is local (ollama) but embeddings are not computed locally")
        if emb == "huggingface" and profile["hf_hub_offline"] not in ("1", "true", "TRUE", "True"):
            warnings.append("HF_HUB_OFFLINE is not 1: the embedding model is downloaded from Hugging Face on first "
                            "use and version-checked online")
        endpoints = [profile["llm_endpoint"], profile["embedding_endpoint"]]
        if any("host.docker.internal" in (e or "") for e in endpoints):
            hosts = compose["services"][SERVICE].get("extra_hosts") or []
            if not any(str(h).startswith("host.docker.internal") for h in hosts):
                warnings.append("host.docker.internal is used but not mapped with extra_hosts; on Linux it will not "
                                "resolve")
    missing_demo = [k for k in DEMO_PASSWORD_VARS if not env.get(k)]
    if missing_demo:
        warnings.append("demo loader will refuse to run: " + ", ".join(missing_demo) + " not set")

    if nginx_text is not None:
        ports = nginx_upstream_ports(nginx_text)
        published = host_port(compose, env)
        if published is not None and ports and any(p != published for p in ports):
            errors.append(f"nginx proxy_pass port(s) {ports} do not match the published CareHub port {published}")
    return errors, warnings


def _mask(key: str, value: str | None) -> str | None:
    if value and any(h in key.upper() for h in SECRET_HINTS):
        return "<set>"
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate CareHub configuration")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--compose", default="docker-compose.yml")
    parser.add_argument("--nginx", default=None)
    args = parser.parse_args(argv)

    compose_path = Path(args.compose)
    try:
        env_file = parse_env(Path(args.env_file).read_text())
        compose = yaml.safe_load(compose_path.read_text())
        effective = effective_environment(compose, env_file.values, compose_path.parent)
        nginx_text = Path(args.nginx).read_text() if args.nginx else None
    except (OSError, EnvParseError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    errors, warnings = validate(env_file, effective, compose, nginx_text)
    profile = ai_profile(effective)
    print("Effective AI configuration (environment only; the Configuration page in the app overrides it):")
    for key, value in profile.items():
        print(f"  {key}: {_mask(key, value) if isinstance(value, str) else value}")
    print(f"  published ports: {published_ports(compose, env_file.values)}")
    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"error: {e}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
