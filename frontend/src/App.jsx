import { useState, useEffect, useCallback, useRef } from 'react'
import AvatarPlayer from './components/AvatarPlayer'
import ChatPanel from './components/ChatPanel'

export default function App() {
  const [guest, setGuest] = useState(null)
  const [messages, setMessages] = useState([])
  const [isLoading, setIsLoading] = useState(false)
  const [rsvpDone, setRsvpDone] = useState(false)
  const avatarApiRef = useRef(null)

  const notionId = new URLSearchParams(window.location.search).get('id')

  useEffect(() => {
    if (!notionId) return
    fetch(`/api/guest/${notionId}`)
      .then((r) => r.json())
      .then((data) => {
        setGuest(data)
        if (data.zusage && data.zusage !== '') setRsvpDone(true)
      })
      .catch(console.error)
  }, [notionId])

  // Avatar exposes either `{guest}` (one-time on session ready) or
  // `{sendText}` (each render once the manager is up). We merge both, and
  // accept `null` so the chat falls back to /api/chat cleanly when the
  // avatar's SDK handle goes away (teardown/error/unmount).
  const handleAvatarReady = useCallback((api) => {
    if (api === null) {
      avatarApiRef.current = null
      return
    }
    if (api?.sendText) {
      avatarApiRef.current = api
    }
    // Use the backend-provided guest preview as a fallback when /api/guest
    // didn't return one (e.g. no `id` query param, or that lookup failed).
    if (api?.guest) {
      setGuest((prev) => prev ?? api.guest)
    }
  }, [])

  const handleAvatarMessage = useCallback((msg) => {
    setMessages((prev) => {
      // The SDK callbacks fire for each final turn; dedupe by string equality
      // with the most recent same-role entry to avoid double-appending.
      const last = prev[prev.length - 1]
      if (last && last.role === msg.role && last.content === msg.content) return prev
      return [...prev, msg]
    })
  }, [])

  // Text-chat fallback / parallel: if the SDK manager is up, route the message
  // through it so the avatar speaks the reply. Otherwise fall back to the
  // legacy /api/chat endpoint.
  const sendText = useCallback(
    async (text) => {
      const trimmed = text.trim()
      if (!trimmed) return
      setMessages((prev) => [...prev, { role: 'user', content: trimmed }])
      setIsLoading(true)
      try {
        if (avatarApiRef.current?.sendText) {
          await avatarApiRef.current.sendText(trimmed)
          // The avatar's onNewMessage will append the assistant reply.
        } else {
          const r = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              message: trimmed,
              notion_id: notionId,
              history: messages,
            }),
          })
          if (!r.ok) throw new Error(`HTTP ${r.status}`)
          const data = await r.json()
          setMessages((prev) => [...prev, { role: 'assistant', content: data.reply }])
          if (data.rsvp_updated && data.rsvp) {
            setRsvpDone(true)
            setGuest((prev) => (prev ? { ...prev, zusage: data.rsvp.zusage } : prev))
          }
        }
      } catch (err) {
        console.error('Chat error:', err)
        setMessages((prev) => [
          ...prev,
          { role: 'assistant', content: 'Entschuldigung, es ist ein Fehler aufgetreten. Bitte versuche es erneut.' },
        ])
      } finally {
        setIsLoading(false)
      }
    },
    [messages, notionId],
  )

  const displayName = guest?.vorname || guest?.name || null

  return (
    <div className="min-h-screen flex flex-col bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950">
      {/* Header */}
      <header className="flex items-center justify-between px-6 py-4 border-b border-gray-800/60">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-violet-500 to-indigo-600 flex items-center justify-center text-xs font-bold">
            VS
          </div>
          <div>
            <div className="text-sm font-semibold text-white leading-tight">VectorSpan</div>
            <div className="text-xs text-gray-400 leading-tight">Release Party · 11. Juli 2026</div>
          </div>
        </div>
        {displayName && (
          <div className="text-sm text-gray-400">
            Hallo, <span className="text-white font-medium">{displayName}</span>
          </div>
        )}
        {rsvpDone && guest?.zusage && (
          <div
            className={`text-xs px-3 py-1 rounded-full font-medium ${
              guest.zusage === 'Ja'
                ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
                : guest.zusage === 'Nein'
                  ? 'bg-red-500/20 text-red-400 border border-red-500/30'
                  : 'bg-yellow-500/20 text-yellow-400 border border-yellow-500/30'
            }`}
          >
            {guest.zusage === 'Ja' ? '✓ Zugesagt' : guest.zusage === 'Nein' ? '✗ Abgesagt' : '~ Mit Vorbehalt'}
          </div>
        )}
      </header>

      {/* Main */}
      <main className="flex-1 flex flex-col lg:flex-row gap-0 lg:gap-6 p-4 lg:p-6 max-w-6xl mx-auto w-full">
        {/* Avatar column */}
        <div className="lg:w-2/5 flex flex-col items-center">
          <AvatarPlayer
            notionId={notionId}
            onReady={handleAvatarReady}
            onMessage={handleAvatarMessage}
          />
          <div className="mt-3 text-center">
            <div className="text-xs text-gray-500">ARIA · Digitale Gastgeberin</div>
            <div className="text-xs text-gray-600 mt-0.5">Sandhauser Str. 20, 13505 Berlin</div>
          </div>
        </div>

        {/* Chat column */}
        <div className="lg:w-3/5 flex flex-col mt-4 lg:mt-0 min-h-0">
          <ChatPanel
            messages={messages}
            onSend={sendText}
            isLoading={isLoading}
            rsvpDone={rsvpDone}
          />
        </div>
      </main>
    </div>
  )
}
