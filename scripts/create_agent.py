#!/usr/bin/env python3
"""Idempotent D-ID Agent provisioning for the Investor Avatar.

What it does:
1. Looks up the agent named `AGENT_NAME` (default "VectorSpan Release Party").
   - If found → updates its LLM/presenter config.
   - If not found → creates it.
   - If the lookup itself fails (network/auth) → exits with an error rather
     than silently creating a duplicate.
2. Ensures a long-lived client-key exists for that agent.
   - If `DID_CLIENT_KEY` is already present in env/.env → reuses it
     (D-ID only reveals the secret once at creation time, so persisting
     it locally is the only way to stay idempotent).
   - Otherwise mints a fresh key, writes it to `.env`, and prints it.
3. Writes `DID_AGENT_ID` and `DID_CLIENT_KEY` back into `.env`.

Env required:
- DID_API_KEY      D-ID API key (read from .env in repo root if present)
- LLM_ENDPOINT_KEY shared secret your /api/llm checks against
- LLM_URL          custom-LLM URL D-ID will POST to (default: https://party.vectorspan.io/api/llm)
- DID_CLIENT_KEY   optional — set to reuse a previously minted key
- AGENT_NAME       optional, default "VectorSpan Release Party"
- ALLOWED_DOMAIN   optional, default "party.vectorspan.io" (CSV for multiple)
- PRESENTER_ID     optional, default "noelle-c2nQB6Cy11"  (D-ID stock presenter)
- DRIVER_ID        optional, default "uM00QMwJ9x"  (only for "talk"/"clip" presenter types)
- VOICE_ID         optional, default "en-US-JennyMultilingualNeural" (multilang Azure)
- PRESENTER_TYPE   optional, default "expressive"  — "expressive" (LiveKit, mic-publish enabled),
                   "clip", or "talk". Expressive is required for publishMicrophoneStream.

Run:
    python scripts/create_agent.py
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import httpx


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def persist_dotenv(path: Path, updates: dict[str, str]) -> None:
    """Idempotently set keys in .env. Updates existing lines in-place and
    appends new keys at the end. Quotes values that contain whitespace or
    shell metacharacters to keep them re-loadable."""
    lines: list[str] = []
    if path.exists():
        lines = path.read_text().splitlines()
    seen: set[str] = set()

    def fmt(k: str, v: str) -> str:
        needs_quote = any(c in v for c in " \t\"'#$`\\")
        if needs_quote:
            v = v.replace("\\", "\\\\").replace('"', '\\"')
            return f'{k}="{v}"'
        return f"{k}={v}"

    for i, raw in enumerate(lines):
        s = raw.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k = s.split("=", 1)[0].strip()
        if k in updates:
            lines[i] = fmt(k, updates[k])
            seen.add(k)
    for k, v in updates.items():
        if k not in seen:
            lines.append(fmt(k, v))
    path.write_text("\n".join(lines) + "\n")


def did_auth_header(api_key: str) -> dict:
    encoded = base64.b64encode(f"{api_key}:".encode()).decode()
    return {
        "Authorization": f"Basic {encoded}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    dotenv_path = repo_root / ".env"
    load_dotenv(dotenv_path)

    api_key = os.getenv("DID_API_KEY", "").strip()
    llm_key = os.getenv("LLM_ENDPOINT_KEY", "").strip()
    existing_client_key = os.getenv("DID_CLIENT_KEY", "").strip() or None
    if not api_key:
        print("ERROR: DID_API_KEY not set (in .env or environment)", file=sys.stderr)
        return 2
    if not llm_key:
        print(
            "ERROR: LLM_ENDPOINT_KEY not set — generate one (e.g. `python -c 'import secrets;print(secrets.token_urlsafe(32))'`) "
            "and put it in .env before provisioning",
            file=sys.stderr,
        )
        return 2

    llm_url = os.getenv("LLM_URL", "https://party.vectorspan.io/api/llm").strip()
    agent_name = os.getenv("AGENT_NAME", "VectorSpan Release Party").strip()
    presenter_id = os.getenv("PRESENTER_ID", "noelle-c2nQB6Cy11").strip()
    driver_id = os.getenv("DRIVER_ID", "uM00QMwJ9x").strip()
    voice_id = os.getenv("VOICE_ID", "en-US-JennyMultilingualNeural").strip()
    presenter_type_raw = os.getenv("PRESENTER_TYPE", "expressive").strip()
    presenter_type = presenter_type_raw.lower()
    if presenter_type not in ("expressive", "clip", "talk"):
        print(
            f"ERROR: PRESENTER_TYPE='{presenter_type_raw}' is not supported. "
            "Use one of: expressive, clip, talk.",
            file=sys.stderr,
        )
        return 2
    allowed_domains = [
        d.strip() for d in os.getenv("ALLOWED_DOMAIN", "party.vectorspan.io").split(",") if d.strip()
    ]
    client_key_name = os.getenv("CLIENT_KEY_NAME", "release-party-client").strip()

    headers = did_auth_header(api_key)

    voice = {"type": "microsoft", "voice_id": voice_id}
    if presenter_type == "expressive":
        # Expressive presenter uses LiveKit (v2) streaming → publishMicrophoneStream available
        presenter = {"type": "expressive", "presenter_id": presenter_id, "voice": voice}
    elif presenter_type == "clip":
        presenter = {"type": "clip", "presenter_id": presenter_id, "driver_id": driver_id, "voice": voice}
    else:  # "talk" — WebRTC v1, no mic publish
        presenter = {"type": "talk", "presenter_id": presenter_id, "driver_id": driver_id, "voice": voice}

    instructions = (
        "Du bist ARIA, der digitale Gastgeber der VectorSpan App Release Party. "
        "Antworten kommen aus dem Custom-LLM-Backend — personalisierter Systemprompt "
        "(Vorname, Sprache, Kontext) wird dort gesetzt. Sprich kurz, freundlich, ohne Markdown."
    )

    llm_config = {
        "provider": "custom",
        "instructions": instructions,
        "temperature": 0.7,
        "custom": {
            "type": "basic",
            "url": llm_url,
            "key": llm_key,
            "streaming": True,
        },
    }

    starter_de = "Hi, ich bin ARIA – schön, dass du da bist!"
    starter_en = "Hi, I'm ARIA – glad you stopped by!"
    starter_fr = "Salut, je suis ARIA – ravie de te voir !"
    starter_it = "Ciao, sono ARIA – che bello vederti!"

    body = {
        "preview_name": agent_name,
        "preview_description": "Personalisierte Begrüßung & RSVP für die VectorSpan App Release Party",
        "presenter": presenter,
        "llm": llm_config,
        "starter_message": [starter_de, starter_en, starter_fr, starter_it],
        "greetings": [starter_de],
        "embed": True,
    }

    env_agent_id = (os.getenv("DID_AGENT_ID") or "").strip()

    with httpx.Client(timeout=30) as client:
        # When DID_AGENT_ID is already in .env, trust it: fetch the agent
        # directly. GET /agents reliably omits some agents we've created, so
        # the name-based fallback below sometimes returns None on existing
        # agents and the script then creates duplicates. The env id closes
        # that hole.
        agent_id: str | None = None
        if env_agent_id:
            existing = _get_agent_by_id(client, headers, env_agent_id)
            if existing:
                agent_id = existing["id"]
                print(f"→ updating existing agent {agent_id} (from DID_AGENT_ID)")
            else:
                print(
                    f"→ DID_AGENT_ID={env_agent_id} not found server-side; falling back to name lookup",
                    file=sys.stderr,
                )

        if agent_id is None:
            existing = _find_agent_by_name(client, headers, agent_name)
            if existing:
                agent_id = existing["id"]
                print(f"→ updating existing agent {agent_id} (by name)")

        if agent_id is not None:
            r = client.patch(f"https://api.d-id.com/agents/{agent_id}", headers=headers, json=body)
            if r.status_code == 403:
                # Most common cause: presenter type not in current plan
                # (expressive requires a paid tier). We refuse to silently
                # downgrade to a different presenter type — that's how we
                # ended up with duplicate "talk" agents next to the real one.
                print(
                    f"ERROR: PATCH agent {agent_id} returned 403 — your D-ID plan may not include "
                    f"presenter type '{presenter_type}'. Aborting without falling back.\n"
                    f"Response: {r.text}",
                    file=sys.stderr,
                )
                return 1
            if r.status_code not in (200, 204):
                print(f"ERROR: update failed {r.status_code}: {r.text}", file=sys.stderr)
                return 1
        else:
            print(f"→ creating new agent '{agent_name}'")
            r = client.post("https://api.d-id.com/agents", headers=headers, json=body)
            if r.status_code == 403:
                print(
                    f"ERROR: POST /agents returned 403 — your D-ID plan may not include "
                    f"presenter type '{presenter_type}'. Aborting without falling back to a different type.\n"
                    f"Response: {r.text}",
                    file=sys.stderr,
                )
                return 1
            if r.status_code not in (200, 201):
                print(f"ERROR: create failed {r.status_code}: {r.text}", file=sys.stderr)
                return 1
            agent_id = r.json().get("id") or r.json().get("agent", {}).get("id")
            if not agent_id:
                print(f"ERROR: response missing agent id: {r.text}", file=sys.stderr)
                return 1

        client_key, created_new_key = _ensure_client_key(
            client, headers, agent_id, client_key_name, allowed_domains, existing_client_key
        )

    # Persist the resolved values so re-running the script (or any other
    # service relying on .env) picks them up automatically. We only touch
    # keys we actually set — never overwrite secrets we didn't compute.
    persist_dotenv(dotenv_path, {"DID_AGENT_ID": agent_id, "DID_CLIENT_KEY": client_key})

    print()
    print("=" * 64)
    print("Agent provisioned.")
    print("=" * 64)
    print(f"DID_AGENT_ID={agent_id}")
    if created_new_key:
        print(f"DID_CLIENT_KEY={client_key}  # newly created — written to {dotenv_path.name}")
    else:
        print(f"DID_CLIENT_KEY=<reused>     # already present in {dotenv_path.name}")
    print(f"LLM_ENDPOINT_KEY={llm_key}  # unchanged — D-ID stores it server-side")
    print()
    return 0


def _get_agent_by_id(client: httpx.Client, headers: dict, agent_id: str) -> dict | None:
    """Look up a known agent directly. Returns the agent dict if it exists,
    None on 404. Raises on auth / network failures so we don't fall through
    to a name lookup that could mint a duplicate."""
    try:
        r = client.get(f"https://api.d-id.com/agents/{agent_id}", headers=headers)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"agent lookup by id failed: {exc}") from exc
    if r.status_code == 200:
        data = r.json()
        # API may return the agent flat or wrapped in {"agent": {...}}
        return data.get("agent") if isinstance(data, dict) and "agent" in data else data
    if r.status_code == 404:
        return None
    raise RuntimeError(
        f"agent lookup by id returned {r.status_code}: {r.text} — refusing to fall through to create"
    )


def _find_agent_by_name(client: httpx.Client, headers: dict, name: str) -> dict | None:
    """Returns the agent dict if found, None if the agent does NOT exist.
    Raises RuntimeError on any other failure (network, auth, etc.) so we
    don't silently fall through to creating a duplicate."""
    try:
        r = client.get("https://api.d-id.com/agents", headers=headers)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"agent lookup failed: {exc}") from exc
    if r.status_code != 200:
        raise RuntimeError(
            f"agent lookup returned {r.status_code}: {r.text} — refusing to create a duplicate"
        )
    data = r.json()
    agents = data.get("agents") if isinstance(data, dict) else data
    if not isinstance(agents, list):
        raise RuntimeError(f"agent lookup: unexpected response shape: {data!r}")
    for a in agents:
        if a.get("preview_name") == name:
            return a
    return None


def _ensure_client_key(
    client: httpx.Client,
    headers: dict,
    agent_id: str,
    key_name: str,
    allowed_domains: list[str],
    existing_env_key: str | None,
) -> tuple[str, bool]:
    """Returns (client_key, created_new). Reuses the value already in env
    (DID_CLIENT_KEY) if present, since D-ID only reveals secrets once at
    creation time and listing keys later does not return the secret."""
    if existing_env_key:
        print(f"→ reusing DID_CLIENT_KEY from environment (length {len(existing_env_key)})")
        return existing_env_key, False

    # Best-effort: check if a key with our name already exists server-side.
    # If it does but the secret isn't available locally, fail with an
    # actionable message rather than silently piling up duplicate keys.
    r = client.get(f"https://api.d-id.com/agents/{agent_id}/client-keys", headers=headers)
    if r.status_code == 200:
        body = r.json()
        keys = body.get("client_keys") if isinstance(body, dict) else body
        if isinstance(keys, list):
            for k in keys:
                if k.get("name") == key_name:
                    secret = k.get("client_key")
                    if secret:
                        print(f"→ server returned secret for key '{key_name}'")
                        return secret, False
                    raise RuntimeError(
                        f"client-key '{key_name}' already exists on agent {agent_id} but the secret is not "
                        "available here (D-ID only returns it once at creation). Either set DID_CLIENT_KEY "
                        "in .env from where it was originally captured, or delete the existing key in the "
                        "D-ID console and re-run this script to mint a fresh one."
                    )

    print(f"→ creating client-key '{key_name}' for domains {allowed_domains}")
    r = client.post(
        f"https://api.d-id.com/agents/{agent_id}/client-keys",
        headers=headers,
        json={"name": key_name, "allowed_domains": allowed_domains},
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"client-key creation failed {r.status_code}: {r.text}")
    return r.json()["client_key"], True


if __name__ == "__main__":
    sys.exit(main())
