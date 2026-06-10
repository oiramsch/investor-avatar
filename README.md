# Investor Avatar — VectorSpan Release Party

Interaktiver Video-Avatar für die VectorSpan App Release Party (Fr, 11. Juli 2026, 17 Uhr, Sandhauser Str. 20, 13505 Berlin). Gäste erhalten per WhatsApp einen personalisierten Link (`?id=<notion_page_id>`), werden persönlich begrüßt und können ihre Zusage direkt über den Avatar eintragen.

## Stack

| Service  | Technologie | Port |
|----------|-------------|------|
| Backend  | Python FastAPI | 8765 |
| Frontend | React + Vite + Tailwind | 8766 |
| Avatar   | D-ID Streaming API (WebRTC) | — |
| KI       | Claude Sonnet (Anthropic) | — |
| Zusagen  | Notion API | — |

## Setup

### 1. Umgebungsvariablen

```bash
cp .env.example .env
# .env befüllen (s.u.)
```

| Variable | Beschreibung |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Anthropic API Key |
| `DID_API_KEY` | D-ID API Key |
| `NOTION_TOKEN` | Notion Integration Token |
| `NOTION_DATABASE_ID` | Notion Datenbank-ID (`37a765e5-dc96-80d8-a1ba-000b4c8c86d9`) |
| `DID_PRESENTER_URL` | Optional: URL des D-ID Presenter-Avatars |

### 2. Docker-Netzwerk anlegen (einmalig)

```bash
docker network create tunnel-net
```

### 3. Starten

```bash
docker compose up -d --build
```

Die App ist dann unter `http://localhost:8766` erreichbar.

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

## Cloudflare Tunnel

Damit Gäste von außen zugreifen können:

1. Cloudflare Tunnel konfigurieren
2. **Public Hostname** → Port `8766` (Frontend mit nginx-Proxy)
3. Der nginx-Container proxied `/api/*` intern an das Backend (Port 8765)

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
