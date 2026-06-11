# Investor Avatar — VectorSpan Release Party

Interaktiver Realtime-Avatar für die VectorSpan App Release Party (Fr, 11. Juli 2026, 17 Uhr, Sandhauser Str. 20, 13505 Berlin). Gäste erhalten per WhatsApp einen personalisierten Link (`?id=<notion_page_id>`), werden persönlich begrüßt und **sprechen** direkt mit dem Avatar (STT → LLM → TTS → Avatar-Rendering); ein Text-Chat bleibt als Fallback.

## Stack

| Service  | Technologie | Port |
|----------|-------------|------|
| Backend  | Python FastAPI (Custom-LLM-Endpoint, Notion-RSVP) | 8765 |
| Frontend | React + Vite + Tailwind + `@d-id/client-sdk` | 8766 |
| Avatar   | D-ID **Agents** (Realtime) | — |
| KI       | Claude Sonnet via OpenAI-kompatibler SSE-Endpoint | — |
| Zusagen  | Notion API | — |

## Architektur

```
Browser ──(WebRTC + Mic)──►  D-ID Agent
                              │
                              │  POST /api/llm  (X-Api-Key, OpenAI-kompatibel)
                              ▼
                       Backend (FastAPI)
                       ├─ Claude SSE-Stream
                       ├─ Notion-Lookup (Gastkontext)
                       └─ update_rsvp Tool → Notion
```

- **D-ID Agents** übernimmt STT, Turn Detection, TTS und das Avatar-Video.
- **Backend `/api/llm`** ist der Custom-LLM-Endpoint, den D-ID je Gesprächsrunde aufruft. Antworten streamen via SSE in OpenAI-Delta-Format.
- **Personalisierung**: Beim Verbindungsstart holt das Frontend einen kurzlebigen Claim-Token (`/api/agent-session?notion_id=…`) und gibt ihn der SDK als `externalId` mit *und* schickt ihn als unsichtbare CLAIM-Handshake-Nachricht. `/api/llm` resolvet den Token → Notion-Gast und merkt sich die Bindung an der D-ID-Distinct-ID für den Rest des Gesprächs.

## Setup

### 1. Umgebungsvariablen

```bash
cp .env.example .env
# .env mit deinen Keys befüllen (s.u.)
```

| Variable | Beschreibung |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Anthropic API Key |
| `DID_API_KEY` | D-ID API Key (Server-seitig) |
| `NOTION_TOKEN` | Notion Integration Token |
| `NOTION_DATABASE_ID` | Notion Datenbank-ID („Einladungen") |
| `DID_AGENT_ID` | wird in Schritt 2 gesetzt |
| `DID_CLIENT_KEY` | wird in Schritt 2 gesetzt |
| `LLM_ENDPOINT_KEY` | shared secret, das D-ID als `X-Api-Key` an `/api/llm` schickt |

`LLM_ENDPOINT_KEY` selber generieren:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Eintragen, **bevor** der Agent provisioniert wird (Schritt 2 hinterlegt den Wert bei D-ID).

### 2. D-ID Agent provisionieren

```bash
pip install httpx
python scripts/create_agent.py
```

Das Skript ist idempotent: Es legt einen Agenten an oder aktualisiert den bestehenden (Name: `VectorSpan Release Party`), trägt Custom-LLM-URL (`https://party.vectorspan.io/api/llm`) und `LLM_ENDPOINT_KEY` bei D-ID ein, und gibt am Ende `DID_AGENT_ID` + `DID_CLIENT_KEY` aus. Diese in `.env` eintragen.

Anpassen über env-Vars beim Aufruf, z. B.:

```bash
LLM_URL=https://staging.vectorspan.io/api/llm \
ALLOWED_DOMAIN=staging.vectorspan.io,party.vectorspan.io \
python scripts/create_agent.py
```

### 3. Docker-Netzwerk anlegen (einmalig)

```bash
docker network create ki-fabrik
```

### 4. Starten

```bash
docker compose up -d --build
```

App unter `http://localhost:8766` erreichbar.

## Nutzung

| URL | Beschreibung |
|-----|-------------|
| `http://localhost:8766/` | Generische Begrüßung auf Deutsch |
| `http://localhost:8766/?id=<notion_page_id>` | Personalisierter Flow für Gast |

### Notion-Datenbank-Schema

Felder der Datenbank „Einladungen":

| Feld | Typ | Beschreibung |
|------|-----|-------------|
| `Name` | title | Nachname |
| `Vorname` | text | Vorname |
| `Sprache` | select | Deutsch / Englisch / Französisch / Italienisch |
| `Zusage` | select | Ja / Nein / mit Vorbehalt |
| `Mitteilung` | text | Optionale Nachricht des Gastes |
| `Ansprache` | text | Interne KI-Verhaltenshinweise (nie dem Gast angezeigt) |
| `Kontext` | text | Persönlicher Hintergrund des Gastes |

## Mikrofon & iOS / Safari

Der Avatar startet **nicht automatisch**. Erst nach Tap auf „Gespräch starten"
fragen wir das Mikrofon ab und öffnen die WebRTC-Verbindung — iOS Safari
erlaubt Mikrofon-Zugriff und Audio-Autoplay nur in direkter Folge einer
Nutzergeste. Sobald die Verbindung steht, erscheint ein „Ton an"-Overlay; ein
weiterer Tap aktiviert den Audio-Output.

Wer kein Mikrofon freigibt, bleibt im **Text-Chat-Fallback**: Eingaben werden
über die SDK an den Agenten geschickt (oder, falls die SDK nicht verbindet,
über `/api/chat` an Claude).

## Migration: was sich geändert hat

| | Vor (Talks) | Jetzt (Agents) |
|-|-------------|----------------|
| Pipeline | Text → /api/chat → D-ID Talks Trigger | Sprache → D-ID Agent → /api/llm (SSE) → TTS |
| Sprache → Text | — (nur Tastatur) | D-ID Built-in STT |
| Latenz | Pro Talk-Request | Token-für-Token streaming |
| Kontext | History pro Chat-Request | D-ID hält Chat-State, `/api/llm` zieht Gast aus Distinct-ID-Mapping |
| RSVP | `update_rsvp` Tool unverändert | `update_rsvp` Tool unverändert |

Die alten WebRTC-Proxy-Routen (`/api/did/streams/*`) sind entfernt – die SDK
übernimmt das selbst.

## Cloudflare Tunnel

Damit Gäste von außen zugreifen können:

1. Cloudflare Tunnel konfigurieren
2. **Public Hostname** → Port `8766` (Frontend mit nginx-Proxy)
3. Der nginx-Container proxied `/api/*` intern an das Backend (Port 8765)
4. Die selbe öffentliche URL muss als `LLM_URL` beim Agenten hinterlegt sein (D-ID ruft `/api/llm` von außen auf!)

## Lokale Entwicklung (ohne Docker)

```bash
# Backend
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8765

# Frontend (separates Terminal)
cd frontend
npm install
npm run dev   # Port 8766, proxied /api/ → localhost:8765
```

Für die Realtime-Pipeline lokal muss D-ID `/api/llm` öffentlich erreichen können — entweder per ngrok/Cloudflare-Tunnel oder durch temporäre Anpassung von `LLM_URL` beim Agent-Provisioning.
