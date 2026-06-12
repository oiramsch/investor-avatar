import { useEffect, useRef, useState, useCallback } from 'react'
import * as didSdk from '@d-id/client-sdk'

const MAX_DEBUG_ERRORS = 5

function speechLang(sprache) {
  return { Deutsch: 'de-DE', Englisch: 'en-US', Französisch: 'fr-FR', Italienisch: 'it-IT' }[sprache] ?? 'de-DE'
}

export default function AvatarPlayer({ notionId, onMessage, onReady }) {
  const videoRef = useRef(null)
  const managerRef = useRef(null)
  const micStreamRef = useRef(null)
  const srcObjectRef = useRef(null)
  const cancelledRef = useRef(false)
  const claimSentRef = useRef(false)
  const recognitionRef = useRef(null)   // SpeechRecognition instance for PTT fallback
  const pttActiveRef = useRef(false)    // true while PTT session is running

  const [sessionConfig, setSessionConfig] = useState(null)
  const [status, setStatus] = useState('idle') // idle | requesting | connecting | live | error | disabled
  const [errorMsg, setErrorMsg] = useState('')
  const [muted, setMuted] = useState(true)
  const [micActive, setMicActive] = useState(false)
  const [micError, setMicError] = useState('')
  // null = unknown (pre-connect), true = LiveKit mic-publish, false = PTT fallback
  const [sdkMicAvailable, setSdkMicAvailable] = useState(null)
  const [speechText, setSpeechText] = useState('')
  const [pttDisabled, setPttDisabled] = useState(false)

  const isDebug = new URLSearchParams(window.location.search).get('debug') === '1'
  const [debug, setDebug] = useState({
    connState: '—',
    lastEvent: '—',
    videoReadyState: -1,
    videoWidth: 0,
    videoHeight: 0,
    videoTracks: 0,
    audioTracks: 0,
    micPermission: '?',
    micPublish: '—',
    claimSent: false,
    claimTs: '',
    claimResult: '—',
    errors: [],
  })

  const pushDebugError = useCallback((msg) => {
    setDebug((prev) => ({
      ...prev,
      errors: [...prev.errors.slice(-(MAX_DEBUG_ERRORS - 1)), msg],
    }))
  }, [])

  const updateVideoDebug = useCallback(() => {
    const v = videoRef.current
    if (!v) return
    const so = v.srcObject
    setDebug((prev) => ({
      ...prev,
      videoReadyState: v.readyState,
      videoWidth: v.videoWidth,
      videoHeight: v.videoHeight,
      videoTracks: so ? so.getVideoTracks().length : 0,
      audioTracks: so ? so.getAudioTracks().length : 0,
    }))
  }, [])

  // 1) Fetch session config from backend on mount
  useEffect(() => {
    cancelledRef.current = false
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
        onReady?.({ guest: null })
      })

    return () => {
      cancelledRef.current = true
      teardown()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [notionId])

  const teardown = useCallback(() => {
    pttActiveRef.current = false
    setPttDisabled(false)
    const rec = recognitionRef.current
    recognitionRef.current = null
    try { rec?.abort() } catch {}
    setSpeechText('')

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

  // 2) User-initiated connect (gesture required for iOS audio + mic)
  const handleStart = async () => {
    if (!sessionConfig || status === 'connecting' || status === 'live') return
    setStatus('requesting')
    setErrorMsg('')
    setMicError('')

    if (isDebug) {
      try {
        const perm = await navigator.permissions.query({ name: 'microphone' })
        setDebug((prev) => ({ ...prev, micPermission: perm.state }))
      } catch {
        setDebug((prev) => ({ ...prev, micPermission: 'api-unavailable' }))
      }
    }

    let micStream
    try {
      micStream = await navigator.mediaDevices.getUserMedia({ audio: true })
      micStreamRef.current = micStream
      setMicActive(true)
      if (isDebug) setDebug((prev) => ({ ...prev, micPermission: 'granted' }))
    } catch (err) {
      const label = `${err.name}: ${err.message}`
      console.warn('getUserMedia denied:', err)
      setMicActive(false)
      setMicError(label)
      if (isDebug) {
        setDebug((prev) => ({
          ...prev,
          micPermission: err.name,
          micPublish: `getUserMedia failed: ${label}`,
        }))
        pushDebugError(`getUserMedia: ${label}`)
      }
    }

    setStatus('connecting')

    try {
      const manager = await didSdk.createAgentManager(sessionConfig.agent_id, {
        auth: { type: 'key', clientKey: sessionConfig.client_key },
        externalId: sessionConfig.claim_token,
        streamOptions: {
          compatibilityMode: 'auto',
          streamWarmup: true,
          outputResolution: 512, // cap resolution for v1 (WebRTC) streams; ignored on LiveKit
        },
        callbacks: {
          onSrcObjectReady(srcObject) {
            srcObjectRef.current = srcObject
            const v = videoRef.current
            if (!v) return
            // Start with the live WebRTC stream; onVideoStateChange will switch to
            // idle_video once the warmup/talking stream stops.
            v.src = ''
            v.srcObject = srcObject
            v.play().catch((e) => console.warn('video.play() failed:', e))
            if (isDebug) updateVideoDebug()
          },

          onVideoStateChange(state) {
            const v = videoRef.current
            if (!v) return
            if (state === 'STOP') {
              // Agent finished talking => show pre-rendered idle video
              const idleUrl = managerRef.current?.agent?.presenter?.idle_video
              v.srcObject = null
              v.loop = true
              v.src = idleUrl || ''
              if (idleUrl) v.play().catch(() => {})
            } else {
              // state === 'START' -- agent is talking => switch to live WebRTC stream
              v.loop = false
              v.src = ''
              v.srcObject = srcObjectRef.current
              v.play().catch((e) => console.warn('video.play() [START]:', e))
            }
            if (isDebug) {
              setDebug((prev) => ({ ...prev, lastEvent: `onVideoStateChange(${state})` }))
              updateVideoDebug()
            }
          },

          onConnectionStateChange(state) {
            if (cancelledRef.current) return
            if (isDebug)
              setDebug((prev) => ({
                ...prev,
                connState: state,
                lastEvent: `onConnectionStateChange(${state})`,
              }))
            if (state === 'connected') {
              setStatus('live')
            } else if (
              // SDK uses 'fail', NOT 'failed'
              state === 'fail' ||
              state === 'closed' ||
              state === 'disconnected'
            ) {
              setStatus('error')
              setErrorMsg('Verbindung verloren.')
              if (isDebug) pushDebugError(`Connection terminal: ${state}`)
            }
          },

          onNewMessage(messages, type) {
            if (!messages || messages.length === 0) return
            const last = messages[messages.length - 1]
            if (
              last.role === 'user' &&
              typeof last.content === 'string' &&
              /^\s*CLAIM:/i.test(last.content)
            ) {
              return
            }
            if (type === 'answer' || type === 'user') {
              onMessage?.({
                role: last.role === 'assistant' ? 'assistant' : 'user',
                content: last.content || '',
              })
            }
          },

          onError(err, errorData) {
            console.error('D-ID SDK error:', err, errorData)
            if (cancelledRef.current) return
            setStatus('error')
            setErrorMsg(err?.message || 'Unbekannter Fehler.')
            if (isDebug) {
              const extra = errorData ? ' | ' + JSON.stringify(errorData) : ''
              pushDebugError(`SDK: ${err?.message}${extra}`)
            }
          },
        },
      })

      if (cancelledRef.current) {
        manager.disconnect().catch(() => {})
        return
      }
      managerRef.current = manager
      await manager.connect()

      // Send CLAIM/greeting AFTER connect() resolves -- calling chat() inside the
      // onConnectionStateChange callback (while connect() is still executing) can
      // silently fail because the internal chat session isn't fully ready yet.
      if (sessionConfig.claim_marker && !claimSentRef.current) {
        claimSentRef.current = true
        const ts = new Date().toISOString()
        if (isDebug)
          setDebug((prev) => ({ ...prev, claimSent: true, claimTs: ts, claimResult: 'pending...' }))
        manager.chat(sessionConfig.claim_marker).then(
          () => {
            if (isDebug) setDebug((prev) => ({ ...prev, claimResult: 'ok' }))
          },
          (err) => {
            console.warn('CLAIM handshake failed:', err)
            if (isDebug) {
              setDebug((prev) => ({ ...prev, claimResult: `error: ${err?.message}` }))
              pushDebugError(`CLAIM: ${err?.message}`)
            }
          },
        )
      }

      // Publish mic after connect.
      // publishMicrophoneStream is only available when the agent uses an Expressive presenter
      // (presenter.type === "expressive"), which selects the LiveKit (v2) streaming manager.
      // Talk/Clip presenters use the WebRTC (v1) manager that lacks publishMicrophoneStream.
      // SDK source: node_modules/@d-id/client-sdk/dist/index.umd.cjs, function Tu(n,e):
      //   ir(n.presenter.type) ? {version:"v2",...bu()} : {version:"v1",...ku(e)}
      //   where ir = n => n === "expressive"
      const isLiveKit = manager.getStreamType?.() === 'livekit'
      setSdkMicAvailable(isLiveKit)
      if (isDebug) setDebug((prev) => ({ ...prev, micPublish: isLiveKit ? 'livekit-checking...' : 'ptt-ready' }))

      if (micStream) {
        if (isLiveKit) {
          try {
            await manager.publishMicrophoneStream(micStream)
            if (isDebug) setDebug((prev) => ({ ...prev, micPublish: 'ok' }))
          } catch (err) {
            console.warn('publishMicrophoneStream failed:', err)
            setMicError(`Mic: ${err?.message || err}`)
            micStream.getTracks().forEach((t) => t.stop())
            micStreamRef.current = null
            setMicActive(false)
            setSdkMicAvailable(false)
            if (isDebug) {
              setDebug((prev) => ({ ...prev, micPublish: `error: ${err?.message}` }))
              pushDebugError(`publishMic: ${err?.message}`)
            }
          }
        } else {
          // Non-LiveKit: release getUserMedia stream so SpeechRecognition can claim the mic on demand
          micStream.getTracks().forEach((t) => t.stop())
          micStreamRef.current = null
          setMicActive(false)
          if (isDebug) setDebug((prev) => ({ ...prev, micPublish: 'ptt-ready' }))
        }
      }
    } catch (err) {
      console.error('D-ID Agents setup failed:', err)
      if (cancelledRef.current) return
      setStatus('error')
      setErrorMsg(err?.message || 'Verbindung fehlgeschlagen.')
      if (isDebug) pushDebugError(`setup: ${err?.message}`)
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
    v.play().catch((e) => console.warn('unmute play() failed:', e))
  }

  const toggleMic = async () => {
    const manager = managerRef.current
    if (!manager) return

    // Push-to-Talk fallback (non-LiveKit / non-Expressive agent)
    if (sdkMicAvailable === false) {
      if (pttDisabled) return
      if (micActive) {
        pttActiveRef.current = false
        const rec = recognitionRef.current
        recognitionRef.current = null
        try { rec?.abort() } catch {}
        setSpeechText('')
        setMicActive(false)
      } else {
        startPTT()
      }
      return
    }

    // SDK mic mode (LiveKit / Expressive agent)
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
      const label = `${err.name}: ${err.message}`
      console.warn('mic toggle: getUserMedia failed:', err)
      setMicError(label)
      if (isDebug) pushDebugError(`toggleMic getUserMedia: ${label}`)
      return
    }
    if (manager.getStreamType?.() !== 'livekit') {
      stream.getTracks().forEach((t) => t.stop())
      return
    }
    try {
      await manager.publishMicrophoneStream(stream)
      micStreamRef.current = stream
      setMicActive(true)
      setMicError('')
      if (isDebug) setDebug((prev) => ({ ...prev, micPublish: 'ok (toggle)' }))
    } catch (err) {
      console.warn('publishMicrophoneStream (toggle) failed:', err)
      stream.getTracks().forEach((t) => t.stop())
      setMicError(`Mic: ${err?.message || err}`)
      if (isDebug) {
        pushDebugError(`toggleMic publish: ${err?.message}`)
        setDebug((prev) => ({ ...prev, micPublish: `toggle error: ${err?.message}` }))
      }
    }
  }

  const startPTT = useCallback(() => {
    const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition
    if (!SpeechRec) {
      setMicError('Spracheingabe nicht verfügbar.')
      return
    }
    const lang = speechLang(sessionConfig?.guest?.sprache)
    const rec = new SpeechRec()
    rec.continuous = false
    rec.interimResults = true
    rec.lang = lang

    rec.onresult = (event) => {
      let interim = ''
      let final = ''
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const t = event.results[i][0].transcript
        if (event.results[i].isFinal) final += t
        else interim += t
      }
      setSpeechText(interim || final)
      if (final.trim()) {
        setSpeechText('')
        managerRef.current?.chat(final.trim())?.catch((err) => {
          console.warn('PTT chat failed:', err)
        })
      }
    }

    rec.onerror = (event) => {
      if (event.error === 'no-speech' || event.error === 'aborted') return
      console.warn('SpeechRecognition error:', event.error)
      const isTerminal = event.error === 'not-allowed' || event.error === 'service-not-available'
      if (isTerminal) {
        pttActiveRef.current = false
        recognitionRef.current = null
        setMicActive(false)
        setPttDisabled(true)
        setMicError('Spracheingabe nicht erlaubt – Mikrofonberechtigung im Browser prüfen.')
      } else {
        setMicError(`PTT: ${event.error}`)
      }
    }

    // Auto-restart after each utterance so the session stays live
    rec.onend = () => {
      if (pttActiveRef.current && recognitionRef.current === rec) {
        try { rec.start() } catch { /* stopped externally */ }
      }
    }

    recognitionRef.current = rec
    pttActiveRef.current = true
    try {
      rec.start()
      setMicActive(true)
      setMicError('')
    } catch (err) {
      console.warn('SpeechRecognition start failed:', err)
      pttActiveRef.current = false
      recognitionRef.current = null
      setMicError(`PTT: ${err.message}`)
    }
  }, [sessionConfig])

  // Expose programmatic send to parent
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

  // Refresh video debug stats while overlay is visible
  useEffect(() => {
    if (!isDebug || status !== 'live') return
    const id = setInterval(updateVideoDebug, 1000)
    return () => clearInterval(id)
  }, [isDebug, status, updateVideoDebug])

  return (
    <div className="relative w-full max-w-xs aspect-[3/4] rounded-2xl overflow-hidden shadow-2xl bg-gray-800 ring-1 ring-gray-700/50">
      {/* Video -- always in DOM; idle_video shows via src, live stream via srcObject */}
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
            {status === 'requesting' ? 'Mikrofon...' : 'Gespräch starten'}
          </button>
          {micError && (
            <p className="text-[10px] text-red-400 text-center max-w-[200px] leading-snug">{micError}</p>
          )}
          <p className="text-[10px] text-gray-600 text-center max-w-[200px] leading-snug">
            Tippe, um Mikrofon und Audio zu erlauben.
          </p>
        </div>
      )}

      {/* Connecting */}
      {status === 'connecting' && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-gradient-to-b from-gray-800 to-gray-900">
          <div className="w-12 h-12 rounded-full border-2 border-violet-500/40 border-t-violet-400 animate-spin" />
          <p className="text-xs text-gray-400">Avatar verbindet ...</p>
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

      {/* Unmute overlay -- iOS Safari only autoplays muted */}
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
        <div className="absolute bottom-3 left-1/2 -translate-x-1/2 flex flex-col items-center gap-1">
          <div className="flex items-center gap-2">
            <div className="flex items-center gap-1.5 bg-black/50 backdrop-blur-sm px-3 py-1 rounded-full">
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
              <span className="text-xs text-gray-300">Live</span>
            </div>
            <button
              type="button"
              onClick={toggleMic}
              disabled={sdkMicAvailable === false && pttDisabled}
              title={micError || undefined}
              className={`flex items-center gap-1.5 px-3 py-1 rounded-full backdrop-blur-sm text-xs transition-colors ${
                micActive
                  ? 'bg-emerald-500/30 text-emerald-300 ring-1 ring-emerald-500/40'
                  : (micError || pttDisabled)
                    ? 'bg-red-500/20 text-red-400 ring-1 ring-red-500/30 opacity-60 cursor-not-allowed'
                    : 'bg-black/50 text-gray-400 hover:bg-black/70'
              }`}
              aria-label={
                sdkMicAvailable === false
                  ? pttDisabled
                    ? 'Spracheingabe gesperrt'
                    : micActive ? 'PTT deaktivieren' : 'PTT aktivieren'
                  : micActive ? 'Mikrofon stumm schalten' : 'Mikrofon aktivieren'
              }
            >
              <svg xmlns="http://www.w3.org/2000/svg" className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3zM19 10v2a7 7 0 01-14 0v-2M12 19v4m-4 0h8" />
              </svg>
              {sdkMicAvailable === false
                ? pttDisabled ? 'PTT gesperrt' : (micActive ? 'PTT an' : 'PTT')
                : (micActive ? 'Mic an' : 'Mic aus')}
            </button>
          </div>
          {sdkMicAvailable === false && pttDisabled && (
            <p className="text-[9px] text-red-400/90 bg-black/60 px-2 py-0.5 rounded-full max-w-[220px] text-center leading-snug">
              Spracheingabe gesperrt – Berechtigung prüfen
            </p>
          )}
          {sdkMicAvailable === false && !pttDisabled && speechText && (
            <p className="text-[9px] text-gray-300/90 bg-black/60 px-2 py-0.5 rounded-full max-w-[200px] truncate italic">
              {speechText}
            </p>
          )}
        </div>
      )}

      {/* Debug overlay -- only visible with ?debug=1 */}
      {isDebug && (status === 'connecting' || status === 'live' || status === 'error') && (
        <div className="absolute top-2 left-2 right-2 bg-black/75 text-[9px] font-mono text-green-300 p-2 rounded-lg leading-[1.4] pointer-events-none select-none z-50">
          <div>conn: <span className="text-white">{debug.connState}</span></div>
          <div>evt: <span className="text-yellow-300">{debug.lastEvent}</span></div>
          <div>
            video: rs=<span className="text-white">{debug.videoReadyState}</span>{' '}
            <span className="text-white">{debug.videoWidth}x{debug.videoHeight}</span>{' '}
            v=<span className="text-white">{debug.videoTracks}</span>{' '}
            a=<span className="text-white">{debug.audioTracks}</span>
          </div>
          <div>mic: perm=<span className="text-white">{debug.micPermission}</span> pub=<span className="text-white">{debug.micPublish}</span></div>
          <div>
            claim:{' '}
            {debug.claimSent
              ? <><span className="text-white">{debug.claimTs.slice(11, 19)}</span> {'->'} <span className={debug.claimResult === 'ok' ? 'text-emerald-400' : 'text-white'}>{debug.claimResult}</span></>
              : <span className="text-gray-500">not sent</span>}
          </div>
          {debug.errors.length > 0 && (
            <div className="mt-1 border-t border-green-900 pt-1">
              {debug.errors.map((e, i) => (
                <div key={i} className="text-red-400 truncate">{e}</div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
