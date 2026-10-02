/* hermes-whatsapp-chat — dashboard tab. Plain IIFE, no build step. */
;(function () {
  'use strict'

  const SDK = window.__HERMES_PLUGIN_SDK__
  if (!SDK) {
    return
  }

  const h = SDK.React.createElement
  const { useState, useEffect, useRef, useToast } = SDK.hooks
  const { Badge, Button, Card, CardContent, Input, Toast } = SDK.components
  const fetchJSON = SDK.fetchJSON
  const API = '/api/plugins/hermes-whatsapp-chat'

  const STATE_LABELS = { new: 'New', in_progress: 'In progress', waiting: 'Waiting', muted: 'Muted', closed: 'Closed' }
  const CHANNEL_LABELS = {
    connected: ['WhatsApp connected', 'success'],
    disconnected: ['WhatsApp disconnected', 'warning'],
    unreachable: ['WhatsApp service stopped', 'destructive']
  }
  const MUTE_PRESETS = [
    [1, '1h'],
    [8, '8h'],
    [24, '24h'],
    [168, '7d']
  ]
  const DROP_MUTE_HOURS = 24
  const WA_JID_RE = /@(s\.whatsapp\.net|lid)$/

  // --- Helpers -------------------------------------------------------------

  const stateLabel = s => STATE_LABELS[s] || s
  const nowSec = () => Math.floor(Date.now() / 1000)
  const chatPath = jid => '/chats/' + encodeURIComponent(jid)

  function fmtAge(s) {
    if (s === null || s === undefined) {
      return ''
    }
    if (s < 60) {
      return 'now'
    }
    if (s < 3600) {
      return Math.floor(s / 60) + 'm'
    }
    if (s < 86400) {
      return Math.floor(s / 3600) + 'h'
    }
    return Math.floor(s / 86400) + 'd'
  }

  function fmtTime(ts) {
    return new Date(ts * 1000).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' })
  }

  // Host errors read "409: {"detail": "..."}" (legacy) or just the detail (ApiError).
  function errorText(err) {
    const msg = String((err && err.message) || err)
    const m = /^(\d{3}):\s*([\s\S]*)$/.exec(msg)
    if (!m) {
      return msg
    }
    try {
      const body = JSON.parse(m[2])
      if (body && typeof body.detail === 'string') {
        return body.detail
      }
    } catch {
      // not JSON: fall through to the raw text
    }
    return m[2]
  }

  function post(path, body) {
    return fetchJSON(API + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    })
  }

  // --- Components ----------------------------------------------------------

  function CardView({ card, onSelect }) {
    const unknownState = !STATE_LABELS[card.state]
    return h(
      'div',
      {
        className: 'wab-card wab-card--' + card.urgency,
        draggable: true,
        onDragStart: e => e.dataTransfer.setData('text/plain', card.chat_jid),
        onClick: () => onSelect(card.chat_jid)
      },
      h(
        'div',
        { className: 'wab-row' },
        h('strong', null, card.contact_name || card.phone),
        card.priority >= 2 ? h(Badge, { tone: 'destructive' }, 'Escalated') : null,
        !card.agent_active ? h(Badge, { tone: 'warning' }, 'Human') : null,
        unknownState ? h(Badge, { tone: 'outline' }, card.state) : null
      ),
      h('div', { className: 'wab-preview' }, card.last_message_preview),
      h(
        'div',
        { className: 'wab-row wab-muted' },
        h('span', null, fmtAge(card.age_seconds)),
        card.unread_count > 0 ? h(Badge, { tone: 'default' }, String(card.unread_count)) : null,
        card.state === 'muted' && card.muted_until ? h('span', null, 'until ' + fmtTime(card.muted_until)) : null
      )
    )
  }

  function ColumnView({ col, count, showCards, onSelect, act }) {
    function onDrop(e) {
      e.preventDefault()
      const jid = e.dataTransfer.getData('text/plain')
      if (!jid) {
        return
      }
      if (col.name === 'muted') {
        act(chatPath(jid) + '/mute', { muted_until: nowSec() + DROP_MUTE_HOURS * 3600 })
      } else {
        act(chatPath(jid) + '/state', { state: col.name })
      }
    }
    return h(
      'div',
      { className: 'wab-col', onDragOver: e => e.preventDefault(), onDrop },
      h(
        'div',
        { className: 'wab-row' },
        h('strong', null, stateLabel(col.name)),
        h(Badge, { tone: 'secondary' }, String(count))
      ),
      showCards ? col.cards.map(card => h(CardView, { key: card.chat_jid, card, onSelect })) : null
    )
  }

  function DetailPanel({ jid, tick, channel, onClose, act }) {
    const [data, setData] = useState(null)
    const [error, setError] = useState(null)
    const [text, setText] = useState('')
    const [sending, setSending] = useState(false)
    const actRef = useRef(act)
    actRef.current = act

    useEffect(() => {
      let cancelled = false
      fetchJSON(API + chatPath(jid)).then(
        d => {
          if (!cancelled) {
            setData(d)
            setError(null)
          }
        },
        e => {
          if (!cancelled) {
            setError(errorText(e))
          }
        }
      )
      return () => {
        cancelled = true
      }
    }, [jid, tick])

    useEffect(() => {
      actRef.current(chatPath(jid) + '/read', {})
    }, [jid])

    useEffect(() => {
      const onKey = e => {
        if (e.key === 'Escape') {
          onClose()
        }
      }
      window.addEventListener('keydown', onKey)
      return () => window.removeEventListener('keydown', onKey)
    }, [onClose])

    if (!data) {
      return h('div', { className: 'wab-detail' }, error ? 'Error: ' + error : 'Loading…')
    }

    const conv = data.conversation
    const allowed = data.allowed_states
    const button = (label, path, body) =>
      h(Button, { key: label, size: 'sm', outlined: true, onClick: () => act(chatPath(jid) + path, body) }, label)
    const actions = []
    if (conv.agent_active || conv.state !== 'in_progress') {
      actions.push(button('Take over', '/takeover', {}))
    }
    if (!conv.agent_active) {
      actions.push(button('Hand back to agent', '/handback', {}))
    }
    actions.push(button(conv.priority >= 2 ? 'De-escalate' : 'Escalate', '/escalate', { escalated: conv.priority < 2 }))
    allowed.filter(s => s !== 'muted').forEach(s => actions.push(button('→ ' + stateLabel(s), '/state', { state: s })))
    if (allowed.includes('muted')) {
      MUTE_PRESETS.forEach(([hours, label]) =>
        actions.push(button('Mute ' + label, '/mute', { muted_until: nowSec() + hours * 3600 }))
      )
    }

    const canReply = channel === 'connected' && WA_JID_RE.test(jid)
    const send = e => {
      e.preventDefault()
      setSending(true)
      act(chatPath(jid) + '/reply', { text })
        .then(ok => ok && setText(''))
        .finally(() => setSending(false))
    }

    return h(
      'div',
      { className: 'wab-detail' },
      h(
        'div',
        { className: 'wab-row' },
        h('strong', null, conv.contact_name || conv.phone),
        h(Button, { size: 'sm', ghost: true, onClick: onClose }, '✕')
      ),
      h('div', { className: 'wab-actions' }, actions),
      h(
        'div',
        { className: 'wab-thread' },
        data.messages.map(m =>
          h(
            'div',
            { key: m.id, className: 'wab-msg wab-msg--' + m.direction },
            m.body,
            h('div', { className: 'wab-muted' }, fmtTime(m.ts))
          )
        )
      ),
      h(
        'form',
        { onSubmit: send },
        h('textarea', {
          className: 'wab-reply',
          rows: 3,
          placeholder: 'Write a reply…',
          value: text,
          onChange: e => setText(e.target.value)
        }),
        h(Button, { size: 'sm', type: 'submit', disabled: !canReply || sending || !text.trim() }, 'Send'),
        !canReply
          ? h(
              'div',
              { className: 'wab-muted' },
              channel !== 'connected' ? 'WhatsApp channel not connected' : 'Demo chat: replies disabled'
            )
          : null
      ),
      h('strong', null, 'History'),
      data.state_history.map(r =>
        h(
          'div',
          { key: r.id, className: 'wab-muted' },
          (r.from_state ? stateLabel(r.from_state) : '—') + ' → ' + stateLabel(r.to_state),
          h('div', null, [r.actor, r.reason, fmtTime(r.at)].filter(Boolean).join(' · '))
        )
      )
    )
  }

  function BoardPage() {
    const [board, setBoard] = useState(null)
    const [stats, setStats] = useState(null)
    const [loadError, setLoadError] = useState(null)
    const [draft, setDraft] = useState('')
    const [query, setQuery] = useState('')
    const [includeClosed, setIncludeClosed] = useState(false)
    const [selected, setSelected] = useState(null)
    const [tick, setTick] = useState(0)
    const { showToast, toast } = useToast()

    useEffect(() => {
      let cancelled = false
      const params = new URLSearchParams({ q: query, include_closed: String(includeClosed) })
      Promise.all([fetchJSON(API + '/board?' + params), fetchJSON(API + '/stats')]).then(
        ([b, s]) => {
          if (!cancelled) {
            setBoard(b)
            setStats(s)
            setLoadError(null)
          }
        },
        e => {
          if (!cancelled) {
            setLoadError(errorText(e))
          }
        }
      )
      return () => {
        cancelled = true
      }
    }, [query, includeClosed, tick])

    useEffect(() => {
      let disposed = false
      let ws = null
      let timer = null
      let backoff = 1000
      const interval = setInterval(() => setTick(t => t + 1), 5000)
      const retry = () => {
        if (disposed) {
          return
        }
        timer = setTimeout(connect, backoff)
        backoff = Math.min(backoff * 2, 30000)
      }
      function connect() {
        SDK.buildWsUrl(API + '/events').then(url => {
          if (disposed) {
            return
          }
          ws = new WebSocket(url)
          ws.onopen = () => {
            backoff = 1000
          }
          ws.onmessage = () => setTick(t => t + 1)
          ws.onclose = retry
        }, retry)
      }
      connect()
      return () => {
        disposed = true
        clearInterval(interval)
        clearTimeout(timer)
        if (ws) {
          ws.close()
        }
      }
    }, [])

    function act(path, body) {
      return post(path, body)
        .then(
          () => true,
          e => {
            showToast(errorText(e), 'error')
            return false
          }
        )
        .finally(() => setTick(t => t + 1))
    }

    if (!board) {
      return loadError
        ? h(Card, null, h(CardContent, null, 'Backend unreachable: ' + loadError))
        : h('div', { className: 'wab-muted' }, 'Loading…')
    }

    const [channelText, channelTone] = CHANNEL_LABELS[stats.channel] || CHANNEL_LABELS.unreachable
    return h(
      'div',
      null,
      h(
        'div',
        { className: 'wab-row wab-toolbar' },
        h(
          'form',
          {
            className: 'wab-row',
            onSubmit: e => {
              e.preventDefault()
              setQuery(draft)
            }
          },
          h(Input, { value: draft, placeholder: 'Search name or phone', onChange: e => setDraft(e.target.value) }),
          h(Button, { size: 'sm', type: 'submit' }, 'Search')
        ),
        h(
          Button,
          { size: 'sm', outlined: true, onClick: () => setIncludeClosed(v => !v) },
          includeClosed ? 'Hide closed' : 'Show closed (' + stats.counts.closed + ')'
        ),
        h(Badge, { tone: channelTone }, channelText)
      ),
      loadError ? h('div', { className: 'wab-warn' }, 'Data may be stale: ' + loadError) : null,
      h(
        'div',
        { className: 'wab-layout' },
        h(
          'div',
          { className: 'wab-board' },
          board.columns.map(col => {
            const hidden = col.name === 'closed' && !includeClosed
            return h(ColumnView, {
              key: col.name,
              col,
              count: hidden ? stats.counts.closed : col.cards.length,
              showCards: !hidden,
              onSelect: setSelected,
              act
            })
          })
        ),
        selected
          ? h(DetailPanel, {
              key: selected,
              jid: selected,
              tick,
              channel: stats.channel,
              onClose: () => setSelected(null),
              act
            })
          : null
      ),
      h(Toast, { toast })
    )
  }

  window.__HERMES_PLUGINS__.register('hermes-whatsapp-chat', BoardPage)
})()
