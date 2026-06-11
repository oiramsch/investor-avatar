#!/usr/bin/env python3
"""Idempotent D-ID Agent provisioning for the Investor Avatar.

What it does:
1. Looks up the agent named `AGENT_NAME` (default "VectorSpan Release Party").
   - If found → updates its LLM/presenter config.
   - If not found → creates it.
2. Ensures a long-lived client-key exists for that agent (creates one if none
   match `CLIENT_KEY_NAME`).
3. Prints `DID_AGENT_ID` and `DID_CLIENT_KEY` for your `.env`.

Env required:
- DID_API_KEY      D-ID API key (read from .env in repo root if present)
- LLM_ENDPOINT_KEY shared secret your /api/llm checks against
- LLM_URL          custom-LLM URL D-ID will POST to (default: https://party.vectorspan.io/api/llm)
- AGENT_NAME       optional, default "VectorSpan Release Party"
- ALLOWED_DOMAIN   optional, default "party.vectorspan.io" (CSV for multiple)
- PRESENTER_ID     optional, default "noelle-c2nQB6Cy11"  (D-ID stock presenter)
- DRIVER_ID        optional, default "uM00QMwJ9x"
- VOICE_ID         optional, default "en-US-JennyMultilingualNeural" (multilang Azure)

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


def did_auth_header(api_key: str) -> dict:
    encoded = base64.b64encode(f"{api_key}:".encode()).decode()
    return {
        "Authorization": f"Basic {encoded}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    load_dotenv(repo_root / ".env")

    api_key = os.getenv("DID_API_KEY", "").strip()
    llm_key = os.getenv("LLM_ENDPOINT_KEY", "").strip()
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
    allowed_domains = [
        d.strip() for d in os.getenv("ALLOWED_DOMAIN", "party.vectorspan.io").split(",") if d.strip()
    ]
    client_key_name = os.getenv("CLIENT_KEY_NAME", "release-party-client").strip()

    headers = did_auth_header(api_key)

    presenter = {
        "type": "talk",
        "presenter_id": presenter_id,
        "driver_id": driver_id,
        "voice": {
            "type": "microsoft",
            "voice_id": voice_id,
        },
    }

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

    with httpx.Client(timeout=30) as client:
        existing = _find_agent_by_name(client, headers, agent_name)
        if existing:
            agent_id = existing["id"]
            print(f"→ updating existing agent {agent_id}")
            r = client.patch(f"https://api.d-id.com/agents/{agent_id}", headers=headers, json=body)
            if r.status_code not in (200, 204):
                print(f"ERROR: update failed {r.status_code}: {r.text}", file=sys.stderr)
                return 1
        else:
            print(f"→ creating new agent '{agent_name}'")
            r = client.post("https://api.d-id.com/agents", headers=headers, json=body)
            if r.status_code not in (200, 201):
                print(f"ERROR: create failed {r.status_code}: {r.text}", file=sys.stderr)
                return 1
            agent_id = r.json().get("id") or r.json().get("agent", {}).get("id")
            if not agent_id:
                print(f"ERROR: response missing agent id: {r.text}", file=sys.stderr)
                return 1

        client_key = _ensure_client_key(client, headers, agent_id, client_key_name, allowed_domains)

    print()
    print("=" * 64)
    print("Agent provisioned. Add these to your .env:")
    print("=" * 64)
    print(f"DID_AGENT_ID={agent_id}")
    print(f"DID_CLIENT_KEY={client_key}")
    print(f"LLM_ENDPOINT_KEY={llm_key}  # unchanged — D-ID stores it server-side")
    print()
    return 0


def _find_agent_by_name(client: httpx.Client, headers: dict, name: str) -> dict | None:
    r = client.get("https://api.d-id.com/agents", headers=headers)
    if r.status_code != 200:
        return None
    data = r.json()
    agents = data.get("agents") if isinstance(data, dict) else data
    if not isinstance(agents, list):
        return None
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
) -> str:
    # Reuse an existing key by name if one is present — keys are stored
    # one-way (creation returns the secret once), so if we don't find it we
    # have to create a new one.
    r = client.get(f"https://api.d-id.com/agents/{agent_id}/client-keys", headers=headers)
    if r.status_code == 200:
        body = r.json()
        keys = body.get("client_keys") if isinstance(body, dict) else body
        if isinstance(keys, list):
            for k in keys:
                if k.get("name") == key_name and k.get("client_key"):
                    print(f"→ reusing client-key '{key_name}'")
                    return k["client_key"]

    print(f"→ creating client-key '{key_name}' for domains {allowed_domains}")
    r = client.post(
        f"https://api.d-id.com/agents/{agent_id}/client-keys",
        headers=headers,
        json={"name": key_name, "allowed_domains": allowed_domains},
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"client-key creation failed {r.status_code}: {r.text}")
    return r.json()["client_key"]


if __name__ == "__main__":
    sys.exit(main())
