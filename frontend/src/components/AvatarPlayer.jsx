import { useEffect, useRef, useState, useCallback } from 'react'
import * as didSdk from '@d-id/client-sdk'

/**
 * Realtime D-ID Agent player.
 *
 * Lifecycle:
 *   1. Mount → fetch `/api/agent-session?notion_id=X` for {agent_id, client_key,
 *      claim_marker}. We don't connect yet — iOS requires a user gesture before
 *      mic/audio can start.
 *   2. User taps "Gespräch starten" → request mic, init `createAgentManager`
 *      with externalId=claim_token, connect, publish mic, and send the CLAIM
 *      handshake message so the backend can bind this session to the guest.
 *   3. Status badge and onNewMessage relay live transcript + assistant text
 *      back to the parent via `onMessage`.
 *
 * The CLAIM handshake message is filtered out via `parent.filterMessage`
 * before display.
 */
export default function AvatarPlayer({ notionId, onMessage, onReady }) {
  const videoRef = useRef(null)
  const managerRef = useRef(null)
  const micStreamRef = useRef(null)
  const cancelledRef = useRef(false)
  const claimSentRef = useRef(false)

  const [sessionConfig, setSessionConfig] = useState(null)
  const [status, setStatus] = useState('idle') // idle | requesting | connecting | live | error | disabled
  const [errorMsg, setErrorMsg] = useState('')
  const [muted, setMuted] = useState(true)
  const [micActive, setMicActive] = useState(false)

  // 1) Fetch session config from backend on mount
  useEffect(() => {
    cancelledRef.current = false
    // New session config → fresh handshake must fire again.
    claimSentRef.current = false
    fetch('/api/agent-session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notion_id: notionId || null }),
    })
      .then(async (r) => {
        if (!r.ok) throw new Error(`agent-session HTTP ${r.status}`)
        return r.json()
      })
      .then((cfg) => {
        if (cancelledRef.current) return
        setSessionConfig(cfg)
        onReady?.({ guest: cfg.guest || null })
      })
      .catch((err) => {
        console.warn('agent-session unavailable:', err)
        if (cancelledRef.current) return
        setStatus('disabled')
        // Still signal "ready" so the parent's text-chat fallback can render.
        onReady?.({ guest: null })
      })

    return () => {
      cancelledRef.current = true
      teardown()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [notionId])

  const teardown = useCallback(() => {
    if (micStreamRef.current) {
      micStreamRef.current.getTracks().forEach((t) => t.stop())
      micStreamRef.current = null
    }
    const m = managerRef.current
    managerRef.current = null
    if (m?.disconnect) {
      m.disconnect().catch(() => {})
    }
  }, [])

  // 2) User-initiated connect (gesture is required for iOS audio + mic)
  const handleStart = async () => {
    if (!sessionConfig || status === 'connecting' || status === 'live') return
    setStatus('requesting')
    setErrorMsg('')

    let micStream
    try {
      micStream = await navigator.mediaDevices.getUserMedia({ audio: true })
      micStreamRef.current = micStream
      setMicActive(true)
    } catch (err) {
      console.warn('getUserMedia denied:', err)
      // Mic denial is non-fatal — user can still text-chat or just listen.
      setMicActive(false)
    }

    setStatus('connecting')

    try {
      const manager = await didSdk.createAgentManager(sessionConfig.agent_id, {
        auth: { type: 'key', clientKey: sessionConfig.client_key },
        // externalId likely surfaces as X-DID-DISTINCT-ID on the Custom-LLM
        // request — the backend uses it as a session→guest lookup key. The
        // CLAIM message below is the belt-and-braces fallback if it doesn't.
        externalId: sessionConfig.claim_token,
        streamOptions: {
          compatibilityMode: 'auto',
          streamWarmup: true,
        },
        callbacks: {
          onSrcObjectReady(srcObject) {
            const v = videoRef.current
            if (!v) return
            v.srcObject = srcObject
            v.play().catch((err) => {
              console.warn('video.play() failed:', err)
            })
          },
          onConnectionStateChange(state) {
            if (cancelledRef.current) return
            if (state === 'connected') {
              setStatus('live')
              // CLAIM handshake — backend reads it from messages[0] and binds
              // the D-ID externalId/distinct-id to the Notion guest.
              if (!claimSentRef.current && sessionConfig.claim_marker) {
                claimSentRef.current = true
                manager.chat(sessionConfig.claim_marker).catch((err) => {
                  console.warn('CLAIM handshake failed:', err)
                })
              }
            } else if (
              // SDK terminal states. The official name is `'failed'` (not
              // `'fail'`); `'closed'` and `'disconnected'` are also terminal
              // for an AgentManager session.
              state === 'failed' ||
              state === 'closed' ||
              state === 'disconnected'
            ) {
              setStatus('error')
              setErrorMsg('Verbindung verloren.')
            }
          },
          onNewMessage(messages, type) {
            if (!messages || messages.length === 0) return
            const last = messages[messages.length - 1]
            // Hide the silent CLAIM handshake from the visible transcript
            if (
              last.role === 'user' &&
              typeof last.content === 'string' &&
              /^\s*CLAIM:/i.test(last.content)
            ) {
              return
            }
            // Only forward terminal turns to keep the parent simple; partials
            // would re-fire many times per second.
            if (type === 'answer' || type === 'user') {
              onMessage?.({
                role: last.role === 'assistant' ? 'assistant' : 'user',
                content: last.content || '',
              })
            }
          },
          onError(err) {
            console.error('D-ID SDK error:', err)
            if (cancelledRef.current) return
            setStatus('error')
            setErrorMsg(err?.message || 'Unbekannter Fehler.')
          },
        },
      })

      if (cancelledRef.current) {
        manager.disconnect().catch(() => {})
        return
      }
      managerRef.current = manager
      await manager.connect()

      if (micStream) {
        if (!manager.publishMicrophoneStream) {
          // SDK variant without mic support — stop the captured tracks so the
          // OS mic indicator doesn't lie about a live recording.
          console.warn('publishMicrophoneStream not supported by this SDK')
          micStream.getTracks().forEach((t) => t.stop())
          micStreamRef.current = null
          setMicActive(false)
        } else {
          try {
            await manager.publishMicrophoneStream(micStream)
            // Publish succeeded — `micActive` may have been set true on capture,
            // we leave it as-is.
          } catch (err) {
            console.warn('publishMicrophoneStream failed:', err)
            micStream.getTracks().forEach((t) => t.stop())
            micStreamRef.current = null
            setMicActive(false)
          }
        }
      }
    } catch (err) {
      console.error('D-ID Agents setup failed:', err)
      if (cancelledRef.current) return
      setStatus('error')
      setErrorMsg(err?.message || 'Verbindung fehlgeschlagen.')
      if (micStreamRef.current) {
        micStreamRef.current.getTracks().forEach((t) => t.stop())
        micStreamRef.current = null
        setMicActive(false)
      }
    }
  }

  const handleUnmute = () => {
    const v = videoRef.current
    if (!v) return
    v.muted = false
    setMuted(false)
    v.play().catch((err) => {
      console.warn('unmute play() failed:', err)
    })
  }

  const toggleMic = async () => {
    const manager = managerRef.current
    if (!manager) return
    if (micActive) {
      if (manager.unpublishMicrophoneStream) {
        await manager.unpublishMicrophoneStream().catch(() => {})
      }
      if (micStreamRef.current) {
        micStreamRef.current.getTracks().forEach((t) => t.stop())
        micStreamRef.current = null
      }
      setMicActive(false)
      return
    }
    let stream
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch (err) {
      console.warn('mic toggle: getUserMedia failed:', err)
      return
    }
    if (!manager.publishMicrophoneStream) {
      // SDK can't publish — don't pretend the mic is live.
      console.warn('publishMicrophoneStream not supported by this SDK')
      stream.getTracks().forEach((t) => t.stop())
      return
    }
    try {
      await manager.publishMicrophoneStream(stream)
      micStreamRef.current = stream
      setMicActive(true)
    } catch (err) {
      console.warn('publishMicrophoneStream failed:', err)
      stream.getTracks().forEach((t) => t.stop())
    }
  }

  // Expose a programmatic send to the parent (text-chat fallback). When the
  // manager isn't live (teardown, error, disconnect), signal `null` so the
  // parent can drop the stale reference and fall back to /api/chat instead of
  // calling into a dead SDK handle.
  useEffect(() => {
    if (!onReady) return
    if (managerRef.current && status === 'live') {
      onReady({
        sendText: (text) => {
          const m = managerRef.current
          if (!m) return Promise.resolve()
          return m.chat(text)
        },
      })
    } else {
      onReady(null)
    }
  }, [status, onReady])

  return (
    <div className="relative w-full max-w-xs aspect-[3/4] rounded-2xl overflow-hidden shadow-2xl bg-gray-800 ring-1 ring-gray-700/50">
      {/* Video */}
      <video
        ref={videoRef}
        autoPlay
        playsInline
        muted={muted}
        className={`w-full h-full object-cover transition-opacity duration-700 ${
          status === 'live' ? 'opacity-100' : 'opacity-0'
        }`}
      />

      {/* Idle (waiting for session config or for user to tap) */}
      {(status === 'idle' || status === 'requesting') && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-gradient-to-b from-gray-800 to-gray-900 p-4">
          <div className="w-20 h-20 rounded-full bg-gradient-to-br from-violet-500 to-indigo-600 flex items-center justify-center text-3xl font-bold shadow-lg">
            A
          </div>
          <p className="text-sm font-medium text-gray-200">ARIA</p>
          <p className="text-xs text-gray-500 text-center">Sprich mit deiner digitalen Gastgeberin</p>
          <button
            type="button"
            onClick={handleStart}
            disabled={!sessionConfig || status === 'requesting'}
            className="mt-2 px-4 py-2 rounded-full bg-violet-600 hover:bg-violet-500 disabled:bg-gray-700 disabled:cursor-not-allowed text-white text-sm font-medium ring-1 ring-white/10 shadow-lg flex items-center gap-2 transition-colors"
          >
            <svg xmlns="http://www.w3.org/2000/svg" className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3zM19 10v2a7 7 0 01-14 0v-2M12 19v4m-4 0h8" />
            </svg>
            {status === 'requesting' ? 'Mikrofon…' : 'Gespräch starten'}
          </button>
          <p className="text-[10px] text-gray-600 text-center max-w-[200px] leading-snug">
            Tippe, um Mikrofon und Audio zu erlauben.
          </p>
        </div>
      )}

      {/* Connecting */}
      {status === 'connecting' && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-gradient-to-b from-gray-800 to-gray-900">
          <div className="w-12 h-12 rounded-full border-2 border-violet-500/40 border-t-violet-400 animate-spin" />
          <p className="text-xs text-gray-400">Avatar verbindet …</p>
        </div>
      )}

      {/* Error / disabled */}
      {(status === 'error' || status === 'disabled') && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-gradient-to-b from-gray-800 to-gray-900 p-4">
          <div className="w-20 h-20 rounded-full bg-gradient-to-br from-violet-500 to-indigo-600 flex items-center justify-center text-3xl font-bold shadow-lg">
            A
          </div>
          <p className="text-sm font-medium text-gray-300">ARIA</p>
          <p className="text-xs text-gray-500 text-center">
            {status === 'disabled'
              ? 'Avatar derzeit nicht verfügbar. Du kannst über den Text-Chat unten weiter mit mir schreiben.'
              : errorMsg || 'Verbindung unterbrochen.'}
          </p>
          {status === 'error' && (
            <button
              type="button"
              onClick={() => {
                claimSentRef.current = false
                teardown()
                setStatus('idle')
              }}
              className="mt-2 text-xs text-violet-400 hover:text-violet-300 underline"
            >
              Erneut versuchen
            </button>
          )}
        </div>
      )}

      {/* Unmute overlay — iOS Safari only autoplays muted */}
      {status === 'live' && muted && (
        <button
          type="button"
          onClick={handleUnmute}
          className="absolute inset-0 flex items-center justify-center bg-black/30 backdrop-blur-[2px] hover:bg-black/40 transition-colors"
          aria-label="Ton einschalten"
        >
          <span className="flex items-center gap-2 bg-black/60 text-white text-sm font-medium px-4 py-2 rounded-full ring-1 ring-white/20 shadow-lg">
            <svg xmlns="http://www.w3.org/2000/svg" className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M15.536 8.464a5 5 0 010 7.072M17.95 6.05a8 8 0 010 11.9M5 8.5h3l5-4v15l-5-4H5v-7z" />
            </svg>
            Ton an
          </span>
        </button>
      )}

      {/* Status badges */}
      {status === 'live' && !muted && (
        <div className="absolute bottom-3 left-1/2 -translate-x-1/2 flex items-center gap-2">
          <div className="flex items-center gap-1.5 bg-black/50 backdrop-blur-sm px-3 py-1 rounded-full">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
            <span className="text-xs text-gray-300">Live</span>
          </div>
          <button
            type="button"
            onClick={toggleMic}
            className={`flex items-center gap-1.5 px-3 py-1 rounded-full backdrop-blur-sm text-xs transition-colors ${
              micActive
                ? 'bg-emerald-500/30 text-emerald-300 ring-1 ring-emerald-500/40'
                : 'bg-black/50 text-gray-400 hover:bg-black/70'
            }`}
            aria-label={micActive ? 'Mikrofon stumm schalten' : 'Mikrofon aktivieren'}
          >
            <svg xmlns="http://www.w3.org/2000/svg" className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3zM19 10v2a7 7 0 01-14 0v-2M12 19v4m-4 0h8" />
            </svg>
            {micActive ? 'Mic an' : 'Mic aus'}
          </button>
        </div>
      )}
    </div>
  )
}
