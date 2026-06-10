import { useEffect, useRef, useState } from 'react'

export default function AvatarPlayer({ onStreamReady }) {
  const videoRef = useRef(null)
  const pcRef = useRef(null)
  const streamIdRef = useRef(null)
  const [status, setStatus] = useState('connecting') // connecting | idle | error | disabled

  useEffect(() => {
    let cancelled = false

    async function init() {
      try {
        const res = await fetch('/api/did/streams', { method: 'POST' })
        if (!res.ok) {
          setStatus('disabled')
          onStreamReady?.({ streamId: null, sessionId: null })
          return
        }
        const data = await res.json()
        if (cancelled) return

        const { id: streamId, session_id: sessionId, offer, ice_servers } = data
        streamIdRef.current = streamId

        const pc = new RTCPeerConnection({ iceServers: ice_servers })
        pcRef.current = pc

        pc.ontrack = (event) => {
          if (videoRef.current && event.streams?.[0]) {
            videoRef.current.srcObject = event.streams[0]
          }
        }

        pc.onconnectionstatechange = () => {
          if (cancelled) return
          const state = pc.connectionState
          if (state === 'connected') setStatus('idle')
          if (state === 'failed' || state === 'disconnected') setStatus('error')
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

        if (!cancelled) {
          onStreamReady?.({ streamId, sessionId })
        }
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
      pcRef.current?.close()
      if (streamIdRef.current) {
        fetch(`/api/did/streams/${streamIdRef.current}`, { method: 'DELETE' }).catch(() => {})
      }
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="relative w-full max-w-xs aspect-[3/4] rounded-2xl overflow-hidden shadow-2xl bg-gray-800 ring-1 ring-gray-700/50">
      {/* Video */}
      <video
        ref={videoRef}
        autoPlay
        playsInline
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

      {/* Status badge */}
      {status === 'idle' && (
        <div className="absolute bottom-3 left-1/2 -translate-x-1/2 flex items-center gap-1.5 bg-black/50 backdrop-blur-sm px-3 py-1 rounded-full">
          <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
          <span className="text-xs text-gray-300">Live</span>
        </div>
      )}
    </div>
  )
}
