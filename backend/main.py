import os
import re
import json
import uuid
import time
import base64
import logging
import secrets
from typing import Optional, List, Any, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

app = FastAPI(title="Investor Avatar Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DID_API_KEY = os.getenv("DID_API_KEY", "")
DID_AGENT_ID = os.getenv("DID_AGENT_ID", "")
DID_CLIENT_KEY = os.getenv("DID_CLIENT_KEY", "")
LLM_ENDPOINT_KEY = os.getenv("LLM_ENDPOINT_KEY", "")
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "37a765e5-dc96-80d8-a1ba-000b4c8c86d9")
DID_PRESENTER_URL = os.getenv(
    "DID_PRESENTER_URL",
    "https://create-images-results.d-id.com/DefaultPresenters/Noelle_f/v1_image.jpeg",
)

DID_BASE_URL = "https://api.d-id.com"
CLAIM_TTL_SECONDS = 600  # 10 minutes — covers slow session setup but won't outlive a real conversation
DISTINCT_TTL_SECONDS = 3 * 3600  # session-binding for a 3-hour party window
CLAIM_MARKER_RE = re.compile(r"^\s*CLAIM:([A-Za-z0-9_\-]{8,128})\s*$", re.IGNORECASE)

VOICE_MAP = {
    "Deutsch": {"type": "microsoft", "voice_id": "de-DE-KatjaNeural"},
    "Englisch": {"type": "microsoft", "voice_id": "en-US-JennyNeural"},
    "Französisch": {"type": "microsoft", "voice_id": "fr-FR-DeniseNeural"},
    "Italienisch": {"type": "microsoft", "voice_id": "it-IT-ElsaNeural"},
}

anthropic_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


# ── In-memory stores ────────────────────────────────────────────────────
#
# CLAIM_STORE: claim_token → {notion_id, expires_at}. Issued when the frontend
# requests a session, consumed when the D-ID Custom-LLM endpoint sees the CLAIM
# marker in the first chat message. Short TTL (10 min).
#
# DISTINCT_STORE: X-DID-DISTINCT-ID → {notion_id, expires_at}. Built up as the
# D-ID pipeline calls our /api/llm for the first time with a claim — keeps the
# guest bound to the D-ID client for the rest of the conversation.
#
# This is intentionally process-local (single backend replica). For
# multi-replica, swap to Redis with the same shape.

CLAIM_STORE: dict[str, dict] = {}
DISTINCT_STORE: dict[str, dict] = {}
# Resolved claims: claim_token → {notion_id, expires_at}. D-ID sends full message
# history on every call, so the CLAIM marker stays in messages[0] after the first
# turn. We keep the resolved mapping here so subsequent turns still find the guest.
CLAIM_RESOLVED_STORE: dict[str, dict] = {}


def _now() -> float:
    return time.time()


def _purge_expired() -> None:
    now = _now()
    for store in (CLAIM_STORE, DISTINCT_STORE, CLAIM_RESOLVED_STORE):
        expired = [k for k, v in store.items() if v.get("expires_at", 0) < now]
        for k in expired:
            store.pop(k, None)


def strip_markdown(text: str) -> str:
    # Inline emphasis is bounded to a single line so list items rendered as
    # `* item` don't get merged across newlines (DOTALL chewed list bullets).
    text = re.sub(r'\*\*([^\n*]+?)\*\*', r'\1', text)
    text = re.sub(r'\*([^\n*]+?)\*', r'\1', text)
    text = re.sub(r'__([^\n_]+?)__', r'\1', text)
    text = re.sub(r'_([^\n_]+?)_', r'\1', text)
    text = re.sub(r'`([^\n`]+?)`', r'\1', text)
    text = re.sub(r'^#+\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*(?:[-+*]|\d+\.)\s+', '', text, flags=re.MULTILINE)
    return text.strip()


# Tiny char-blacklist for SSE chunks. Regex-based `strip_markdown` is unsafe
# on streamed chunks because Anthropic sends them in tiny pieces (`**`, `Wort`,
# `**`) and emphasis spans get split across boundaries — the regex misses and
# the markers reach the TTS. Since the system prompt explicitly forbids markdown,
# stray characters here are residue, not real formatting, so we just drop them.
_MD_CHUNK_CHARS = str.maketrans("", "", "*_`#")


def strip_markdown_chunk(text: str) -> str:
    return text.translate(_MD_CHUNK_CHARS)


def did_headers() -> dict:
    encoded = base64.b64encode(f"{DID_API_KEY}:".encode()).decode()
    return {
        "Authorization": f"Basic {encoded}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }


async def get_notion_guest(page_id: str) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=notion_headers(),
            timeout=10,
        )
    if r.status_code != 200:
        raise HTTPException(status_code=404, detail="Gast nicht gefunden")
    page = r.json()
    props = page.get("properties", {})

    def get_title(p: dict) -> str:
        items = p.get("title", [])
        return "".join(t.get("plain_text", "") for t in items)

    def get_text(p: dict) -> str:
        items = p.get("rich_text", [])
        return "".join(t.get("plain_text", "") for t in items)

    def get_select(p: dict) -> str:
        s = p.get("select")
        return s.get("name", "") if s else ""

    return {
        "id": page_id,
        "name": get_title(props.get("Name", {})),
        "vorname": get_text(props.get("Vorname", {})),
        "sprache": get_select(props.get("Sprache", {})) or "Deutsch",
        "zusage": get_select(props.get("Zusage", {})),
        "ansprache": get_text(props.get("Ansprache", {})),
        "kontext": get_text(props.get("Kontext", {})),
        "frage_mitbringen": get_text(props.get("Frage Mitbringen", {})),
        "frage_andere_uhrzeit": get_text(props.get("Frage Andere Uhrzeit", {})),
    }


async def update_notion_rsvp(page_id: str, zusage: str, mitteilung: Optional[str] = None) -> bool:
    props: dict = {"Zusage": {"select": {"name": zusage}}}
    if mitteilung:
        props["Mitteilung"] = {"rich_text": [{"text": {"content": mitteilung}}]}
    async with httpx.AsyncClient() as client:
        r = await client.patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=notion_headers(),
            json={"properties": props},
            timeout=10,
        )
    return r.status_code == 200


async def append_notion_mitteilung(page_id: str, text: str) -> bool:
    """Append `text` to the Mitteilung field (never overwrites).
    Format: "[TT.MM. HH:MM] text\n..." (UTC timestamps)."""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=notion_headers(),
            timeout=10,
        )
    if r.status_code != 200:
        return False
    props = r.json().get("properties", {})
    existing = "".join(
        t.get("plain_text", "")
        for t in props.get("Mitteilung", {}).get("rich_text", [])
    )
    timestamp = time.strftime("[%d.%m. %H:%M]", time.gmtime())
    entry = f"{timestamp} {text}"
    new_text = f"{existing}\n{entry}".strip() if existing else entry
    async with httpx.AsyncClient() as client:
        r = await client.patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=notion_headers(),
            json={"properties": {"Mitteilung": {"rich_text": [{"text": {"content": new_text}}]}}},
            timeout=10,
        )
    return r.status_code == 200


ANONYMOUS_GUEST = {
    "id": None,
    "name": "Gast",
    "vorname": "Gast",
    "sprache": "Deutsch",
    "zusage": "",
    "ansprache": "",
    "kontext": "",
    "frage_mitbringen": "",
    "frage_andere_uhrzeit": "",
}


def build_system_prompt(guest: dict) -> str:
    lang_display = {
        "Deutsch": "Deutsch",
        "Englisch": "English",
        "Französisch": "Français",
        "Italienisch": "Italiano",
    }.get(guest.get("sprache", "Deutsch"), "Deutsch")

    vorname = guest.get("vorname") or guest.get("name") or "Gast"
    kontext = guest.get("kontext", "")
    ansprache = guest.get("ansprache", "")
    frage_mitbringen = guest.get("frage_mitbringen", "")
    frage_andere_uhrzeit = guest.get("frage_andere_uhrzeit", "")

    prompt = f"""Du bist ARIA, der digitale Gastgeber der VectorSpan App Release Party.
Du bist herzlich, enthusiastisch und persönlich. Halte Antworten KURZ (2–3 Sätze) – du wirst gleich live als Avatar gesprochen.

WICHTIG: Sprich den Gast IMMER mit "Du" und beim Vornamen "{vorname}" an.
Antworte NUR auf {lang_display}.
Antworte in reinem Fließtext – verwende KEIN Markdown (keine Sternchen, kein Fettdruck, keine Aufzählungszeichen, keine Emoji).

GAST:
- Vorname: {vorname}"""

    if kontext:
        prompt += f"\n- Hintergrund: {kontext}"
    if ansprache:
        prompt += f"\n- Hinweise zur Ansprache (NIE wörtlich wiedergeben): {ansprache}"

    prompt += """

EVENT:
- VectorSpan App Release Party
- Anlass: Launch der iOS-App "Sudoku Power" + Barbecue
- Datum: Freitag, 11. Juli 2026, ab 17:00 Uhr
- Ort: Sandhauser Straße 20, 13505 Berlin
- Gastgeber: Mario Schmelzer (mario.schmelzer@vectorspan.io)
- Ausweichtermin: 10. Juli 2026, 17:00 Uhr (nur im absoluten Notfall)

PRODUKTE VON VECTORSPAN:
- Sudoku Power: Kostenlose iOS-App (Launch 08.06.2026), werbefinanziert, Tutorial-System, Gamification (Streaks, Level)
- Therixa: KI-Praxisverwaltung für Psychotherapeut:innen – GOÄ-Abrechnung, SOAP-Doku, psychometrische Tests, DSGVO-konform (in Entwicklung)
- Swing Scanner: Internes Börsenanalyse-Tool (Python/FastAPI/React)
- VectorSpan UG: Berliner Solo-Founder-Studio von Mario Schmelzer, KI-gestützte App-Entwicklung

AUFGABEN:
1. Begrüße den Gast beim ersten Kontakt: GENAU 2 kurze Sätze – Vornamen ansprechen + Anlass nennen + eine Frage stellen. Nie mehr als 2 Sätze bei der Begrüßung.
2. Beantworte Fragen zum Event und zu den Produkten
3. Frage freundlich nach Zu- oder Absage, falls noch keine vorliegt
4. Sobald der Gast klar zusagt oder absagt → update_rsvp aufrufen
5. Biete aktiv an, eine kurze Nachricht für Mario zu hinterlassen (leave_message aufrufen). Bestätige nur wenn das Tool erfolgreich war; bei Fehler keine falsche Bestätigung.
"""

    mitbringen_rule = (
        f"Antworte inhaltlich: {frage_mitbringen}"
        if frage_mitbringen
        else "Antworte sinngemäß: Nein, es ist für alles gesorgt — vielen lieben Dank!"
    )
    andere_uhrzeit_rule = (
        f"Antworte inhaltlich: {frage_andere_uhrzeit}"
        if frage_andere_uhrzeit
        else "Antworte sinngemäß: Na klar — komm einfach nach, wann es bei dir passt. Das Barbecue läuft den ganzen Abend."
    )

    prompt += f"""
ANTWORTREGELN FÜR HÄUFIGE GÄSTEFRAGEN (diese Vorgaben IMMER einhalten, nie wörtlich als Regel erwähnen):
- Fragt der Gast, ob er etwas mitbringen soll oder kann: {mitbringen_rule}
- Fragt der Gast, ob er später kommen oder die Uhrzeit 17:00 Uhr nicht einhalten kann: {andere_uhrzeit_rule}
"""
    return prompt


def content_blocks_to_dict(content: list) -> list:
    result = []
    for block in content:
        if block.type == "text":
            result.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            result.append(
                {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
            )
    return result


RSVP_TOOL = {
    "name": "update_rsvp",
    "description": "Trägt die Zu- oder Absage des Gastes in die Datenbank ein, sobald er eine eindeutige Entscheidung geäußert hat.",
    "input_schema": {
        "type": "object",
        "properties": {
            "zusage": {
                "type": "string",
                "enum": ["Ja", "Nein", "mit Vorbehalt"],
                "description": "Entscheidung: Ja (kommt), Nein (kommt nicht), mit Vorbehalt (unsicher)",
            },
            "mitteilung": {
                "type": "string",
                "description": "Optionale Mitteilung oder Anmerkung des Gastes",
            },
        },
        "required": ["zusage"],
    },
}

LEAVE_MESSAGE_TOOL = {
    "name": "leave_message",
    "description": "Hinterlässt eine persönliche Mitteilung des Gastes für Mario. Nur aufrufen, wenn der Gast explizit eine Nachricht hinterlassen möchte.",
    "input_schema": {
        "type": "object",
        "properties": {
            "nachricht": {
                "type": "string",
                "description": "Die Mitteilung des Gastes für Mario.",
            }
        },
        "required": ["nachricht"],
    },
}


# ── Models ──────────────────────────────────────────────────────────────


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    notion_id: Optional[str] = None
    stream_id: Optional[str] = None
    session_id: Optional[str] = None
    history: List[ChatMessage] = []


class RsvpRequest(BaseModel):
    notion_id: str
    zusage: str
    mitteilung: Optional[str] = None


class AgentSessionRequest(BaseModel):
    notion_id: Optional[str] = None


# ── Helpers: extract claim & resolve guest ──────────────────────────────


def _extract_claim_token(messages: list) -> Optional[str]:
    """Return the claim token from the first user message that looks like
    `CLAIM:<token>`, else None. We only inspect early messages so a guest
    typing "CLAIM:..." mid-conversation can't hijack the binding."""
    for msg in messages[:2]:
        if (msg.get("role") or "").lower() != "user":
            continue
        content = msg.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text += block.get("text", "")
        m = CLAIM_MARKER_RE.match(text)
        if m:
            return m.group(1)
    return None


async def _resolve_guest_for_request(messages: list, distinct_id: Optional[str]) -> Tuple[dict, bool]:
    """Resolve the guest for an incoming Custom-LLM request.
    Returns (guest_dict, is_first_turn). Side-effects: on first consumption
    populates CLAIM_RESOLVED_STORE (claim→notion_id, long TTL) and DISTINCT_STORE.

    `is_first_turn` is true ONLY when we actually consumed a claim from
    CLAIM_STORE this request. D-ID sends the full history on every call, so
    the CLAIM marker will sit in `messages[0]` forever — we must not let its
    presence alone trigger the greeting injection.

    Personalization on turns 2+: CLAIM marker is still in the history but
    CLAIM_STORE entry is already consumed. We look up CLAIM_RESOLVED_STORE
    first (works when X-DID-DISTINCT-ID is absent), then DISTINCT_STORE."""
    _purge_expired()

    claim = _extract_claim_token(messages)
    notion_id: Optional[str] = None
    claim_consumed = False

    if claim:
        entry = CLAIM_STORE.pop(claim, None)
        if entry and entry.get("expires_at", 0) >= _now():
            claim_consumed = True
            notion_id = entry.get("notion_id")
            if notion_id:
                # Persist for subsequent turns — D-ID includes full history each call
                CLAIM_RESOLVED_STORE[claim] = {
                    "notion_id": notion_id,
                    "expires_at": _now() + DISTINCT_TTL_SECONDS,
                }
            if distinct_id and notion_id:
                DISTINCT_STORE[distinct_id] = {
                    "notion_id": notion_id,
                    "expires_at": _now() + DISTINCT_TTL_SECONDS,
                }

    # Subsequent turns: claim present in history but already consumed from CLAIM_STORE
    if not notion_id and claim:
        entry = CLAIM_RESOLVED_STORE.get(claim)
        if entry and entry.get("expires_at", 0) >= _now():
            notion_id = entry.get("notion_id")

    if not notion_id and distinct_id:
        entry = DISTINCT_STORE.get(distinct_id)
        if entry and entry.get("expires_at", 0) >= _now():
            notion_id = entry.get("notion_id")

    guest = dict(ANONYMOUS_GUEST)
    if notion_id:
        try:
            guest = await get_notion_guest(notion_id)
        except Exception:
            logger.exception("Failed to load Notion guest %s — falling back to anonymous", notion_id)

    return guest, claim_consumed


def _messages_for_anthropic(messages: list, inject_greeting: bool) -> list:
    """Convert D-ID's `{role, content, created_at}` messages into Anthropic
    `{role, content}`. CLAIM marker messages are ALWAYS dropped (D-ID sends
    full history on every turn — Claude must never see the handshake). When
    `inject_greeting` is true (first consumed claim) and the CLAIM was the
    only user input so far, we append a benign opener so the model still
    produces the greeting turn."""
    out = []
    skipped_claim = False
    for msg in messages:
        role = (msg.get("role") or "").lower()
        if role not in ("user", "assistant"):
            continue
        raw_content = msg.get("content")
        if isinstance(raw_content, list):
            text = "".join(
                b.get("text", "") for b in raw_content if isinstance(b, dict) and b.get("type") == "text"
            )
        else:
            text = raw_content or ""
        if role == "user" and CLAIM_MARKER_RE.match(text or ""):
            skipped_claim = True
            continue
        out.append({"role": role, "content": text})

    if inject_greeting and skipped_claim and not any(m["role"] == "user" for m in out):
        out.append(
            {"role": "user", "content": "Bitte begrüße mich jetzt herzlich. Stell dich und das Event kurz vor."}
        )
    return out


# ── SSE serializer (OpenAI-compatible) ──────────────────────────────────


def _sse_chunk(chunk_id: str, content: str = "", finish_reason: Optional[str] = None) -> bytes:
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(_now()),
        "choices": [
            {
                "index": 0,
                "delta": {"content": content} if content else {},
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


# ── Routes ──────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/guest/{notion_page_id}")
async def get_guest(notion_page_id: str):
    guest = await get_notion_guest(notion_page_id)
    return {
        "id": guest["id"],
        "name": guest["name"],
        "vorname": guest["vorname"],
        "sprache": guest["sprache"],
        "zusage": guest["zusage"],
    }


@app.post("/api/agent-session")
async def agent_session(req: AgentSessionRequest):
    """Issue D-ID Agent connection params + a short-lived claim token bound
    to the requesting guest. The frontend hands the claim to D-ID via the
    first chat() message; our Custom-LLM endpoint consumes it and binds the
    D-ID distinct-id to the guest for the rest of the conversation."""
    if not DID_AGENT_ID or not DID_CLIENT_KEY:
        raise HTTPException(
            status_code=503,
            detail="DID_AGENT_ID / DID_CLIENT_KEY not configured — run scripts/create_agent.py",
        )

    _purge_expired()

    notion_id = (req.notion_id or "").strip() or None
    guest_preview: Optional[dict] = None
    if notion_id:
        try:
            guest = await get_notion_guest(notion_id)
            guest_preview = {
                "vorname": guest.get("vorname") or guest.get("name") or "Gast",
                "sprache": guest.get("sprache", "Deutsch"),
            }
        except Exception:
            # Bad ID: continue with anonymous binding rather than erroring out;
            # the SDK still works, just without personalization.
            notion_id = None

    claim_token = f"ctx_{secrets.token_urlsafe(18)}"
    CLAIM_STORE[claim_token] = {
        "notion_id": notion_id,
        "expires_at": _now() + CLAIM_TTL_SECONDS,
    }

    return {
        "agent_id": DID_AGENT_ID,
        "client_key": DID_CLIENT_KEY,
        "claim_token": claim_token,
        "claim_marker": f"CLAIM:{claim_token}",
        "guest": guest_preview,
    }


@app.post("/api/llm")
async def custom_llm(
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key"),
    x_did_distinct_id: Optional[str] = Header(default=None, alias="X-DID-DISTINCT-ID"),
    x_did_agent_id: Optional[str] = Header(default=None, alias="X-DID-AGENT-ID"),
):
    """OpenAI-compatible Custom LLM endpoint per D-ID Custom LMs spec.
    https://docs.d-id.com/docs/custom-llms — request: {messages, options, stream},
    response: SSE with {choices: [{delta: {content}}]} chunks."""
    if not LLM_ENDPOINT_KEY:
        raise HTTPException(status_code=503, detail="LLM_ENDPOINT_KEY not configured")
    # secrets.compare_digest is constant-time — avoids leaking key length via
    # response-time differences if D-ID ever rotates and someone probes.
    if not x_api_key or not secrets.compare_digest(x_api_key, LLM_ENDPOINT_KEY):
        raise HTTPException(status_code=401, detail="Invalid X-Api-Key")

    body = await request.json()
    messages = body.get("messages") or []
    stream = bool(body.get("stream", True))

    guest, is_first_turn = await _resolve_guest_for_request(messages, x_did_distinct_id)
    anthropic_messages = _messages_for_anthropic(messages, inject_greeting=is_first_turn)
    if not anthropic_messages:
        anthropic_messages = [
            {"role": "user", "content": "Bitte begrüße mich jetzt herzlich. Stell dich und das Event kurz vor."}
        ]

    system_prompt = build_system_prompt(guest)
    notion_id = guest.get("id")
    tools = [RSVP_TOOL, LEAVE_MESSAGE_TOOL] if notion_id else []

    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    async def stream_response():
        try:
            async for chunk in _run_anthropic_with_tools(
                system_prompt, anthropic_messages, tools, notion_id, chunk_id
            ):
                yield chunk
        except Exception:
            logger.exception("Custom LLM streaming failed")
            yield _sse_chunk(chunk_id, content="Entschuldigung, bei mir ist gerade ein Fehler passiert.")
            yield _sse_chunk(chunk_id, finish_reason="error")

    if stream:
        return StreamingResponse(
            stream_response(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # tell nginx not to buffer SSE
            },
        )

    # Non-streaming fallback per spec: {"content": "..."}
    full = ""
    async for chunk in _run_anthropic_with_tools(
        system_prompt, anthropic_messages, tools, notion_id, chunk_id
    ):
        # Re-parse our own chunks to reconstruct the text. Not the cheapest
        # path but D-ID always sends stream=true in practice.
        try:
            line = chunk.decode().strip()
            if line.startswith("data:"):
                obj = json.loads(line[5:].strip())
                full += obj["choices"][0].get("delta", {}).get("content", "") or ""
        except Exception:
            continue
    return {"content": full}


async def _run_anthropic_with_tools(
    system_prompt: str,
    anthropic_messages: list,
    tools: list,
    notion_id: Optional[str],
    chunk_id: str,
):
    """Streams Claude text as OpenAI-compatible SSE chunks. If Claude calls
    update_rsvp or leave_message, we apply the Notion patch and replay the
    conversation with the tool_result so the model produces the final spoken reply."""

    create_kwargs: dict[str, Any] = dict(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system_prompt,
        messages=list(anthropic_messages),
    )
    if tools:
        create_kwargs["tools"] = tools

    async with anthropic_client.messages.stream(**create_kwargs) as s:
        async for text in s.text_stream:
            text = strip_markdown_chunk(text)
            if text:
                yield _sse_chunk(chunk_id, content=text)
        final = await s.get_final_message()

    if final.stop_reason == "tool_use":
        tool_block = next((b for b in final.content if b.type == "tool_use"), None)
        if tool_block and notion_id:
            inputs = tool_block.input or {}
            tool_result_content = ""
            is_error = False

            if tool_block.name == "update_rsvp":
                rsvp_ok = False
                try:
                    rsvp_ok = bool(
                        await update_notion_rsvp(
                            notion_id, inputs.get("zusage", ""), inputs.get("mitteilung")
                        )
                    )
                except Exception:
                    logger.exception("update_notion_rsvp failed")
                tool_result_content = (
                    "RSVP wurde erfolgreich gespeichert."
                    if rsvp_ok
                    else "Fehler beim Speichern des RSVP. Bitte sag dem Gast, dass es gerade nicht klappt und wir es später erneut versuchen."
                )
                if not rsvp_ok:
                    is_error = True

            elif tool_block.name == "leave_message":
                msg_ok = False
                try:
                    msg_ok = bool(
                        await append_notion_mitteilung(notion_id, inputs.get("nachricht", ""))
                    )
                except Exception:
                    logger.exception("append_notion_mitteilung failed")
                tool_result_content = (
                    "Mitteilung wurde erfolgreich für Mario hinterlassen."
                    if msg_ok
                    else "Fehler beim Speichern der Mitteilung. Bitte sag dem Gast, dass es gerade nicht klappt."
                )
                if not msg_ok:
                    is_error = True

            if tool_result_content:
                tool_result_block: dict = {
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": tool_result_content,
                }
                if is_error:
                    tool_result_block["is_error"] = True

                followup_messages = list(anthropic_messages)
                followup_messages.append(
                    {"role": "assistant", "content": content_blocks_to_dict(final.content)}
                )
                followup_messages.append({"role": "user", "content": [tool_result_block]})
                followup_kwargs = dict(create_kwargs)
                followup_kwargs["messages"] = followup_messages

                async with anthropic_client.messages.stream(**followup_kwargs) as s2:
                    async for text in s2.text_stream:
                        text = strip_markdown_chunk(text)
                        if text:
                            yield _sse_chunk(chunk_id, content=text)

    yield _sse_chunk(chunk_id, finish_reason="stop")


# ── Legacy text-chat endpoint (fallback when SDK is unavailable) ────────


@app.post("/api/chat")
async def chat(req: ChatRequest):
    guest = None
    if req.notion_id:
        try:
            guest = await get_notion_guest(req.notion_id)
        except Exception:
            pass

    if guest is None:
        guest = dict(ANONYMOUS_GUEST)

    system_prompt = build_system_prompt(guest)

    if req.message == "__INIT__":
        messages: List[dict] = [
            {"role": "user", "content": "Bitte begrüße mich jetzt herzlich. Stell dich und das Event kurz vor."}
        ]
    else:
        messages = [{"role": m.role, "content": m.content} for m in req.history]
        messages.append({"role": "user", "content": req.message})

    tools = [RSVP_TOOL, LEAVE_MESSAGE_TOOL] if req.notion_id else []

    create_kwargs: dict[str, Any] = dict(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system_prompt,
        messages=messages,
    )
    if tools:
        create_kwargs["tools"] = tools

    response = await anthropic_client.messages.create(**create_kwargs)

    reply_text = ""
    rsvp_updated = False
    rsvp_result = None

    if response.stop_reason == "tool_use":
        tool_block = next((b for b in response.content if b.type == "tool_use"), None)
        if tool_block and req.notion_id:
            inputs = tool_block.input or {}
            tool_result_content = ""
            is_error = False

            if tool_block.name == "update_rsvp":
                rsvp_updated = False
                try:
                    rsvp_updated = await update_notion_rsvp(
                        req.notion_id, inputs.get("zusage", ""), inputs.get("mitteilung")
                    )
                except Exception:
                    logger.exception("update_notion_rsvp failed (chat)")
                rsvp_result = inputs
                tool_result_content = (
                    "RSVP wurde erfolgreich gespeichert."
                    if rsvp_updated
                    else "Fehler beim Speichern des RSVP. Bitte sag dem Gast, dass es gerade nicht klappt und wir es später erneut versuchen."
                )
                if not rsvp_updated:
                    is_error = True

            elif tool_block.name == "leave_message":
                msg_ok = False
                try:
                    msg_ok = await append_notion_mitteilung(
                        req.notion_id, inputs.get("nachricht", "")
                    )
                except Exception:
                    logger.exception("append_notion_mitteilung failed (chat)")
                tool_result_content = (
                    "Mitteilung wurde erfolgreich für Mario hinterlassen."
                    if msg_ok
                    else "Fehler beim Speichern der Mitteilung. Bitte sag dem Gast, dass es gerade nicht klappt."
                )
                if not msg_ok:
                    is_error = True

            if tool_result_content:
                tool_result_block: dict = {
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": tool_result_content,
                }
                if is_error:
                    tool_result_block["is_error"] = True
                messages.append({"role": "assistant", "content": content_blocks_to_dict(response.content)})
                messages.append({"role": "user", "content": [tool_result_block]})
                create_kwargs["messages"] = messages
                response2 = await anthropic_client.messages.create(**create_kwargs)
                for block in response2.content:
                    if hasattr(block, "text"):
                        reply_text += block.text
            else:
                for block in response.content:
                    if hasattr(block, "text"):
                        reply_text += block.text
        else:
            for block in response.content:
                if hasattr(block, "text"):
                    reply_text += block.text
    else:
        for block in response.content:
            if hasattr(block, "text"):
                reply_text += block.text

    return {"reply": reply_text, "rsvp_updated": rsvp_updated, "rsvp": rsvp_result}


@app.patch("/api/rsvp")
async def update_rsvp(req: RsvpRequest):
    ok = await update_notion_rsvp(req.notion_id, req.zusage, req.mitteilung)
    if not ok:
        raise HTTPException(status_code=500, detail="Notion-Update fehlgeschlagen")
    return {"success": True}
