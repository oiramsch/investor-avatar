import os
import base64
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Any
from anthropic import AsyncAnthropic

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
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "37a765e5-dc96-80d8-a1ba-000b4c8c86d9")
DID_PRESENTER_URL = os.getenv(
    "DID_PRESENTER_URL",
    "https://create-images-results.d-id.com/DefaultPresenters/Noelle_f/v1_image.jpeg",
)

DID_BASE_URL = "https://api.d-id.com"

VOICE_MAP = {
    "Deutsch": {"type": "microsoft", "voice_id": "de-DE-KatjaNeural"},
    "Englisch": {"type": "microsoft", "voice_id": "en-US-JennyNeural"},
    "Französisch": {"type": "microsoft", "voice_id": "fr-FR-DeniseNeural"},
    "Italienisch": {"type": "microsoft", "voice_id": "it-IT-ElsaNeural"},
}

anthropic_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


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

    prompt = f"""Du bist ARIA, der digitale Gastgeber der VectorSpan App Release Party.
Du bist herzlich, enthusiastisch und persönlich. Halte Antworten kurz (2-4 Sätze) – du wirst als Videoavatar dargestellt.

WICHTIG: Sprich den Gast IMMER mit "Du" und beim Vornamen "{vorname}" an.
Antworte NUR auf {lang_display}.

GAST:
- Vorname: {vorname}"""

    if kontext:
        prompt += f"\n- Hintergrund: {kontext}"
    if ansprache:
        prompt += f"\n- Hinweise zur Ansprache: {ansprache}"

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
1. Begrüße den Gast herzlich beim ersten Kontakt
2. Beantworte Fragen zum Event und zu den Produkten
3. Frage freundlich nach Zu- oder Absage, falls noch keine vorliegt
4. Sobald der Gast klar zusagt oder absagt → update_rsvp aufrufen
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


# ── Models ────────────────────────────────────────────────────────────────────


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


# ── Routes ────────────────────────────────────────────────────────────────────


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


@app.post("/api/did/streams")
async def did_create_stream():
    if not DID_API_KEY:
        raise HTTPException(status_code=503, detail="D-ID API key not configured")
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{DID_BASE_URL}/talks/streams",
            headers=did_headers(),
            json={"source_url": DID_PRESENTER_URL, "config": {"stitch": True}},
            timeout=30,
        )
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()


@app.post("/api/did/streams/{stream_id}/sdp")
async def did_set_sdp(stream_id: str, body: dict):
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{DID_BASE_URL}/talks/streams/{stream_id}/sdp",
            headers=did_headers(),
            json=body,
            timeout=30,
        )
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json() if r.text else {}


@app.post("/api/did/streams/{stream_id}/ice")
async def did_send_ice(stream_id: str, body: dict):
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{DID_BASE_URL}/talks/streams/{stream_id}/ice",
            headers=did_headers(),
            json=body,
            timeout=30,
        )
    if r.status_code not in (200, 201, 204):
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json() if r.text and r.text.strip() != "" else {}


@app.delete("/api/did/streams/{stream_id}")
async def did_close_stream(stream_id: str, body: dict = {}):
    async with httpx.AsyncClient() as client:
        await client.delete(
            f"{DID_BASE_URL}/talks/streams/{stream_id}",
            headers=did_headers(),
            json=body,
            timeout=30,
        )
    return {}


@app.post("/api/chat")
async def chat(req: ChatRequest):
    guest = None
    if req.notion_id:
        try:
            guest = await get_notion_guest(req.notion_id)
        except Exception:
            pass

    if guest is None:
        guest = {
            "id": None,
            "name": "Gast",
            "vorname": "Gast",
            "sprache": "Deutsch",
            "zusage": "",
            "ansprache": "",
            "kontext": "",
        }

    system_prompt = build_system_prompt(guest)

    # __INIT__ is a frontend signal to generate the opening greeting
    if req.message == "__INIT__":
        messages: List[dict] = [
            {"role": "user", "content": "Bitte begrüße mich jetzt herzlich. Stell dich und das Event kurz vor."}
        ]
    else:
        messages = [{"role": m.role, "content": m.content} for m in req.history]
        messages.append({"role": "user", "content": req.message})

    tools = [RSVP_TOOL] if req.notion_id else []

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
        if tool_block and tool_block.name == "update_rsvp" and req.notion_id:
            inputs = tool_block.input
            rsvp_updated = await update_notion_rsvp(
                req.notion_id, inputs["zusage"], inputs.get("mitteilung")
            )
            rsvp_result = inputs

            messages.append({"role": "assistant", "content": content_blocks_to_dict(response.content)})
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_block.id,
                            "content": "RSVP wurde erfolgreich gespeichert.",
                        }
                    ],
                }
            )
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

    # Trigger D-ID avatar to speak the reply
    if req.stream_id and req.session_id and reply_text and DID_API_KEY:
        voice = VOICE_MAP.get(guest.get("sprache", "Deutsch"), VOICE_MAP["Deutsch"])
        try:
            async with httpx.AsyncClient() as http_client:
                await http_client.post(
                    f"{DID_BASE_URL}/talks/streams/{req.stream_id}/talks",
                    headers=did_headers(),
                    json={
                        "script": {
                            "type": "text",
                            "input": reply_text,
                            "provider": voice,
                        },
                        "config": {"stitch": True},
                        "session_id": req.session_id,
                    },
                    timeout=30,
                )
        except Exception:
            pass  # Don't break chat if D-ID talk fails

    return {"reply": reply_text, "rsvp_updated": rsvp_updated, "rsvp": rsvp_result}


@app.patch("/api/rsvp")
async def update_rsvp(req: RsvpRequest):
    ok = await update_notion_rsvp(req.notion_id, req.zusage, req.mitteilung)
    if not ok:
        raise HTTPException(status_code=500, detail="Notion-Update fehlgeschlagen")
    return {"success": True}
