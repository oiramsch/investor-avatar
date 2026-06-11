import { useEffect, useRef, useState } from 'react'

export default function AvatarPlayer({ onStreamReady }) {
  const videoRef = useRef(null)
  const pcRef = useRef(null)
  const streamIdRef = useRef(null)
  const disconnectTimerRef = useRef(null)
  const [status, setStatus] = useState('connecting') // connecting | idle | error | disabled
  const [muted, setMuted] = useState(true)

  useEffect(() => {
    let cancelled = false

    async function init() {
      try {
        const res = await fetch('/api/did/streams', { method: 'POST' })
        if (!res.ok) {
          if (!cancelled) {
            setStatus('disabled')
            onStreamReady?.({ streamId: null, sessionId: null })
          }
          return
        }
        const data = await res.json()

        const { id: streamId, session_id: sessionId, offer, ice_servers } = data
        streamIdRef.current = streamId

        // Unmount race: if the component was unmounted while POST was inflight,
        // the cleanup function ran before streamIdRef was set — close the stream
        // explicitly here so D-ID doesn't keep an orphan session open.
        if (cancelled) {
          fetch(`/api/did/streams/${streamId}`, { method: 'DELETE' }).catch(() => {})
          return
        }

        const pc = new RTCPeerConnection({ iceServers: ice_servers })
        pcRef.current = pc

        pc.ontrack = (event) => {
          if (videoRef.current && event.streams?.[0]) {
            videoRef.current.srcObject = event.streams[0]
            // iOS Safari needs an explicit play() after srcObject is set.
            // Autoplay is only allowed because the element is muted.
            videoRef.current.play().catch((err) => {
              console.warn('video.play() failed:', err)
            })
          }
        }

        let streamReadyFired = false
        pc.onconnectionstatechange = () => {
          if (cancelled) return
          const state = pc.connectionState
          if (state === 'connected') {
            if (disconnectTimerRef.current) {
              clearTimeout(disconnectTimerRef.current)
              disconnectTimerRef.current = null
            }
            setStatus('idle')
            if (!streamReadyFired) {
              streamReadyFired = true
              onStreamReady?.({ streamId, sessionId })
            }
          } else if (state === 'failed') {
            setStatus('error')
            if (!streamReadyFired) {
              streamReadyFired = true
              onStreamReady?.({ streamId: null, sessionId: null })
            }
          } else if (state === 'disconnected') {
            // iOS Safari reports transient 'disconnected' during normal playback.
            // Only treat as terminal if it stays disconnected for >5s.
            if (disconnectTimerRef.current) return
            disconnectTimerRef.current = setTimeout(() => {
              disconnectTimerRef.current = null
              if (cancelled) return
              if (pc.connectionState === 'disconnected') {
                setStatus('error')
                if (!streamReadyFired) {
                  streamReadyFired = true
                  onStreamReady?.({ streamId: null, sessionId: null })
                }
              }
            }, 5000)
          }
        }

        pc.onicecandidate = async ({ candidate }) => {
          if (!candidate || cancelled) return
          try {
            await fetch(`/api/did/streams/${streamId}/ice`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                candidate: candidate.candidate,
                sdpMid: candidate.sdpMid,
                sdpMLineIndex: candidate.sdpMLineIndex,
                session_id: sessionId,
              }),
            })
          } catch (_) {}
        }

        // D-ID uses Janus — add a data channel so the connection completes
        pc.createDataChannel('JanusDataChannel')

        await pc.setRemoteDescription(new RTCSessionDescription(offer))
        const answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        await fetch(`/api/did/streams/${streamId}/sdp`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            answer: { type: answer.type, sdp: answer.sdp },
            session_id: sessionId,
          }),
        })

      } catch (err) {
        if (!cancelled) {
          console.warn('D-ID WebRTC setup failed:', err)
          setStatus('disabled')
          onStreamReady?.({ streamId: null, sessionId: null })
        }
      }
    }

    init()

    return () => {
      cancelled = true
      if (disconnectTimerRef.current) {
        clearTimeout(disconnectTimerRef.current)
        disconnectTimerRef.current = null
      }
      pcRef.current?.close()
      if (streamIdRef.current) {
        fetch(`/api/did/streams/${streamIdRef.current}`, { method: 'DELETE' }).catch(() => {})
      }
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const handleUnmute = () => {
    const v = videoRef.current
    if (!v) return
    v.muted = false
    setMuted(false)
    v.play().catch((err) => {
      console.warn('unmute play() failed:', err)
    })
  }

  return (
    <div className="relative w-full max-w-xs aspect-[3/4] rounded-2xl overflow-hidden shadow-2xl bg-gray-800 ring-1 ring-gray-700/50">
      {/* Video */}
      <video
        ref={videoRef}
        autoPlay
        playsInline
        muted={muted}
        className={`w-full h-full object-cover transition-opacity duration-700 ${
          status === 'idle' ? 'opacity-100' : 'opacity-0'
        }`}
      />

      {/* Connecting state */}
      {status === 'connecting' && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3">
          <div className="w-12 h-12 rounded-full border-2 border-violet-500/40 border-t-violet-400 animate-spin" />
          <p className="text-xs text-gray-400">Avatar verbindet …</p>
        </div>
      )}

      {/* Error / disabled state — show static placeholder */}
      {(status === 'error' || status === 'disabled') && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-gradient-to-b from-gray-800 to-gray-900">
          <div className="w-20 h-20 rounded-full bg-gradient-to-br from-violet-500 to-indigo-600 flex items-center justify-center text-3xl font-bold shadow-lg">
            A
          </div>
          <p className="text-sm font-medium text-gray-300">ARIA</p>
          <p className="text-xs text-gray-500">Digitale Gastgeberin</p>
        </div>
      )}

      {/* Unmute overlay — required because iOS Safari only autoplays muted */}
      {status === 'idle' && muted && (
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

      {/* Status badge */}
      {status === 'idle' && !muted && (
        <div className="absolute bottom-3 left-1/2 -translate-x-1/2 flex items-center gap-1.5 bg-black/50 backdrop-blur-sm px-3 py-1 rounded-full">
          <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
          <span className="text-xs text-gray-300">Live</span>
        </div>
      )}
    </div>
  )
}
