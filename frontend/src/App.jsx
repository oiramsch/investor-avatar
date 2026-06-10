import { useState, useEffect, useCallback, useRef } from 'react'
import AvatarPlayer from './components/AvatarPlayer'
import ChatPanel from './components/ChatPanel'

export default function App() {
  const [guest, setGuest] = useState(null)
  const [messages, setMessages] = useState([])
  const [streamInfo, setStreamInfo] = useState(null)
  const [isLoading, setIsLoading] = useState(false)
  const [rsvpDone, setRsvpDone] = useState(false)
  const initSentRef = useRef(false)

  const notionId = new URLSearchParams(window.location.search).get('id')

  useEffect(() => {
    if (notionId) {
      fetch(`/api/guest/${notionId}`)
        .then((r) => r.json())
        .then((data) => {
          setGuest(data)
          if (data.zusage && data.zusage !== '') setRsvpDone(true)
        })
        .catch(console.error)
    }
  }, [notionId])

  const sendMessage = useCallback(
    async (text, currentHistory) => {
      const isInit = text === '__INIT__'
      const history = currentHistory ?? messages

      if (!isInit) {
        setMessages((prev) => [...prev, { role: 'user', content: text }])
      }
      setIsLoading(true)

      try {
        const r = await fetch('/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            message: text,
            notion_id: notionId,
            stream_id: streamInfo?.streamId ?? null,
            session_id: streamInfo?.sessionId ?? null,
            history: isInit ? [] : history,
          }),
        })
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        const data = await r.json()

        setMessages((prev) => [...prev, { role: 'assistant', content: data.reply }])

        if (data.rsvp_updated && data.rsvp) {
          setRsvpDone(true)
          setGuest((prev) => (prev ? { ...prev, zusage: data.rsvp.zusage } : prev))
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
    [messages, notionId, streamInfo],
  )

  // Send greeting once WebRTC connection is established (or falls back to disabled mode)
  useEffect(() => {
    if (streamInfo !== null && !initSentRef.current) {
      initSentRef.current = true
      sendMessage('__INIT__', [])
    }
  }, [streamInfo, sendMessage])

  const handleStreamReady = useCallback((info) => {
    setStreamInfo(info)
  }, [])

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
          <AvatarPlayer onStreamReady={handleStreamReady} />
          <div className="mt-3 text-center">
            <div className="text-xs text-gray-500">ARIA · Digitale Gastgeberin</div>
            <div className="text-xs text-gray-600 mt-0.5">Sandhauser Str. 20, 13505 Berlin</div>
          </div>
        </div>

        {/* Chat column */}
        <div className="lg:w-3/5 flex flex-col mt-4 lg:mt-0 min-h-0">
          <ChatPanel
            messages={messages}
            onSend={(text) => sendMessage(text, messages)}
            isLoading={isLoading}
            rsvpDone={rsvpDone}
          />
        </div>
      </main>
    </div>
  )
}
