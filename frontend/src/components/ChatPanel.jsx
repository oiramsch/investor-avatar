import { useState, useRef, useEffect } from 'react'

function stripMarkdown(text) {
  // Bounded to a single line so list items rendered as `* foo` don't get
  // their content merged across newlines (the prior /gs variant did).
  return text
    .replace(/\*\*([^\n*]+?)\*\*/g, '$1')
    .replace(/\*([^\n*]+?)\*/g, '$1')
    .replace(/__([^\n_]+?)__/g, '$1')
    .replace(/_([^\n_]+?)_/g, '$1')
    .replace(/`([^\n`]+?)`/g, '$1')
    .replace(/^#+\s+/gm, '')
    .replace(/^\s*(?:[-+*]|\d+\.)\s+/gm, '')
}

function Message({ role, content }) {
  const isUser = role === 'user'
  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      {!isUser && (
        <div className="w-7 h-7 rounded-full bg-gradient-to-br from-violet-500 to-indigo-600 flex items-center justify-center text-xs font-bold mr-2 mt-0.5 flex-shrink-0">
          A
        </div>
      )}
      <div
        className={`max-w-[80%] px-4 py-2.5 rounded-2xl text-sm leading-relaxed ${
          isUser
            ? 'bg-violet-600 text-white rounded-br-sm'
            : 'bg-gray-800 text-gray-100 rounded-bl-sm'
        }`}
      >
        {stripMarkdown(content)}
      </div>
    </div>
  )
}

function TypingIndicator() {
  return (
    <div className="flex justify-start">
      <div className="w-7 h-7 rounded-full bg-gradient-to-br from-violet-500 to-indigo-600 flex items-center justify-center text-xs font-bold mr-2 mt-0.5 flex-shrink-0">
        A
      </div>
      <div className="bg-gray-800 px-4 py-3 rounded-2xl rounded-bl-sm flex gap-1.5 items-center">
        <span className="w-1.5 h-1.5 bg-gray-400 rounded-full animate-bounce [animation-delay:0ms]" />
        <span className="w-1.5 h-1.5 bg-gray-400 rounded-full animate-bounce [animation-delay:150ms]" />
        <span className="w-1.5 h-1.5 bg-gray-400 rounded-full animate-bounce [animation-delay:300ms]" />
      </div>
    </div>
  )
}

export default function ChatPanel({ messages, onSend, isLoading, rsvpDone }) {
  const [input, setInput] = useState('')
  const bottomRef = useRef(null)
  const inputRef = useRef(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, isLoading])

  const handleSend = () => {
    const text = input.trim()
    if (!text || isLoading) return
    setInput('')
    onSend(text)
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  const suggestedReplies =
    !rsvpDone && messages.length > 0
      ? ['Ich komme gerne!', 'Ich kann leider nicht.', 'Noch nicht sicher.']
      : []

  return (
    <div className="flex flex-col h-full min-h-[400px] lg:min-h-0 bg-gray-900/50 rounded-2xl ring-1 ring-gray-800/60 overflow-hidden">
      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-4 space-y-3 scrollbar-thin">
        {messages.length === 0 && !isLoading && (
          <div className="h-full flex items-center justify-center">
            <p className="text-gray-600 text-sm">Begrüßung wird geladen…</p>
          </div>
        )}
        {messages.map((msg, i) => (
          <Message key={i} role={msg.role} content={msg.content} />
        ))}
        {isLoading && <TypingIndicator />}
        <div ref={bottomRef} />
      </div>

      {/* Quick replies */}
      {suggestedReplies.length > 0 && !isLoading && (
        <div className="px-4 pb-2 flex gap-2 flex-wrap">
          {suggestedReplies.map((reply) => (
            <button
              key={reply}
              onClick={() => onSend(reply)}
              className="text-xs px-3 py-1.5 rounded-full bg-gray-800 hover:bg-gray-700 text-gray-300 hover:text-white border border-gray-700/50 transition-colors"
            >
              {reply}
            </button>
          ))}
        </div>
      )}

      {/* Input */}
      <div className="p-3 border-t border-gray-800/60">
        <div className="flex gap-2 items-end">
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Nachricht eingeben…"
            rows={1}
            disabled={isLoading}
            className="flex-1 bg-gray-800 text-gray-100 placeholder-gray-500 text-sm px-4 py-2.5 rounded-xl resize-none focus:outline-none focus:ring-1 focus:ring-violet-500/50 disabled:opacity-50 scrollbar-thin"
            style={{ maxHeight: '120px', overflowY: 'auto' }}
          />
          <button
            onClick={handleSend}
            disabled={!input.trim() || isLoading}
            className="flex-shrink-0 w-10 h-10 rounded-xl bg-violet-600 hover:bg-violet-500 disabled:bg-gray-700 disabled:cursor-not-allowed text-white flex items-center justify-center transition-colors"
            aria-label="Senden"
          >
            <svg xmlns="http://www.w3.org/2000/svg" className="w-4 h-4 rotate-90" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 19V5m0 0l-7 7m7-7l7 7" />
            </svg>
          </button>
        </div>
        <p className="text-xs text-gray-600 mt-1.5 text-center">
          Enter zum Senden · Shift+Enter für neue Zeile
        </p>
      </div>
    </div>
  )
}
