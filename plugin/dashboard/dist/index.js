/* hermes-whatsapp-chat — dashboard tab (v2). Plain IIFE, no build step. */
;(function () {
  'use strict'

  const SDK = window.__HERMES_PLUGIN_SDK__
  if (!SDK) {
    return
  }

  const h = SDK.React.createElement
  const { useState, useEffect, useRef, useMemo, useContext, createContext, useToast } = SDK.hooks
  const { Badge, Button, Card, CardContent, Input, Toast } = SDK.components
  const fetchJSON = SDK.fetchJSON
  const API = '/api/plugins/hermes-whatsapp-chat'
  // Bump together with API_VERSION in wa_core/service.py: the UI is newer than a backend that reports less.
  const REQUIRED_API_VERSION = 10
  const RESTART_TITLE = 'Restart the Hermes dashboard to finish installing or updating WhatsApp Chat'
  const SERVICE_START_TIMEOUT_MS = 30000

  const STATE_LABELS = { new: 'New', in_progress: 'In progress', waiting: 'Waiting', muted: 'Muted', closed: 'Closed' }
  const ACCOUNT_STATE_LABELS = {
    stopped: 'Stopped',
    starting: 'Starting…',
    pairing: 'Preparing pairing…',
    qr: 'Scan QR code',
    connecting: 'Connecting…',
    connected: 'Connected',
    disconnected: 'Disconnected',
    logged_out: 'Logged out',
    error: 'Error'
  }
  const ACCOUNT_STATE_TONES = {
    stopped: 'secondary',
    starting: 'warning',
    pairing: 'warning',
    qr: 'warning',
    connecting: 'warning',
    connected: 'success',
    disconnected: 'destructive',
    logged_out: 'secondary',
    error: 'destructive'
  }
  const ACCOUNT_COLORS = ['green', 'blue', 'red', 'orange', 'yellow', 'purple', 'teal', 'pink', 'gray']
  const HISTORY_MODES = [
    ['recent', 'Import recent history'],
    ['full', 'Import full history'],
    ['off', 'No history import']
  ]
  const DEFAULT_MUTE_PRESETS = [1, 8, 24, 168]
  const DEFAULT_DROP_MUTE_HOURS = 24
  const SERVICE_FRESH_SECONDS = 15
  const MAX_LIST_LIMIT = 200
  const WA_JID_RE = /@(s\.whatsapp\.net|lid|g\.us)$/
  const LIST_LABELS = {
    admin: 'Admin',
    work: 'Work',
    personal: 'Personal',
    unclassified: 'To classify',
    ignored: 'Ignored'
  }
  const LIST_TONES = {
    admin: 'secondary',
    work: 'default',
    personal: 'success',
    unclassified: 'warning',
    ignored: 'outline'
  }
  // Group senders get a stable colour from this fixed list (hashed by sender jid).
  const SENDER_COLORS = [
    'var(--color-primary)',
    'var(--color-success)',
    'var(--color-warning)',
    'var(--color-destructive)',
    'color-mix(in srgb, var(--color-warning) 50%, var(--color-destructive))',
    'color-mix(in srgb, var(--color-destructive) 50%, var(--color-primary))',
    'color-mix(in srgb, var(--color-success) 50%, var(--color-primary))'
  ]
  const TABS = [
    ['board', 'Board'],
    ['chats', 'Chats'],
    ['numbers', 'Numbers']
  ]

  const AppCtx = createContext(null)

  // --- Helpers -------------------------------------------------------------

  const stateLabel = s => STATE_LABELS[s] || s
  const nowSec = () => Math.floor(Date.now() / 1000)

  function qs(params) {
    const parts = []
    Object.keys(params).forEach(k => {
      const v = params[k]
      if (v === null || v === undefined || v === '') {
        return
      }
      parts.push(k + '=' + encodeURIComponent(String(v)))
    })
    return parts.length ? '?' + parts.join('&') : ''
  }

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

  function fmtSize(bytes) {
    if (typeof bytes !== 'number') {
      return ''
    }
    if (bytes < 1024) {
      return bytes + ' B'
    }
    if (bytes < 1024 * 1024) {
      return Math.round(bytes / 1024) + ' KB'
    }
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB'
  }

  function fmtHours(hours) {
    return hours >= 48 && hours % 24 === 0 ? hours / 24 + 'd' : hours + 'h'
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

  function errorStatus(err) {
    if (err && typeof err.status === 'number' && err.status > 0) {
      return err.status
    }
    const m = /^(\d{3}):/.exec(String((err && err.message) || err))
    return m ? Number(m[1]) : null
  }

  // Toast text: "HTTP <status>: <detail>"; 404/405 usually mean Hermes still runs the previous backend.
  function failureText(err) {
    const status = errorStatus(err)
    const hint = status === 404 || status === 405 ? ' — restart Hermes to load the updated plugin' : ''
    return (status ? 'HTTP ' + status + ': ' : '') + (errorText(err) || 'Request failed') + hint
  }

  function request(method, path, body) {
    const init = { method }
    if (body !== undefined) {
      init.headers = { 'Content-Type': 'application/json' }
      init.body = JSON.stringify(body)
    }
    return fetchJSON(API + path, init)
  }

  const convName = c =>
    c.contact_name ||
    (c.is_group ? 'Group' : '') ||
    (c.phone
      ? '+' + String(c.phone).replace(/^\+/, '')
      : /@lid$/.test(c.chat_jid || '')
        ? 'Unknown contact'
        : c.chat_jid)

  // Group, list and silenced marks of a conversation card.
  function convMarks(c) {
    return [
      c.is_group ? h(Badge, { key: 'group', tone: 'outline' }, 'Group') : null,
      c.list ? h(Badge, { key: 'list', tone: LIST_TONES[c.list] || 'outline' }, LIST_LABELS[c.list] || c.list) : null,
      c.silenced ? h(Badge, { key: 'silenced', tone: 'outline' }, 'Silenced') : null
    ]
  }

  function senderColor(key) {
    let hash = 0
    String(key)
      .split('')
      .forEach(ch => {
        hash = (hash * 31 + ch.charCodeAt(0)) >>> 0
      })
    return SENDER_COLORS[hash % SENDER_COLORS.length]
  }

  // Phone number of a group sender when WhatsApp told us (digits, or a phone jid), else ''.
  function senderPhone(m) {
    if (m.sender_phone) {
      return '+' + String(m.sender_phone).replace(/^\+/, '')
    }
    const jid = String(m.sender_jid || '')
    return /^\d+@s\.whatsapp\.net$/.test(jid) ? '+' + jid.split('@')[0] : ''
  }

  function senderLabel(m) {
    return m.sender_name || senderPhone(m) || 'Unknown participant'
  }

  function dotClass(color) {
    return 'wab-dot wab-dot--' + (/^[a-z]+$/.test(String(color)) ? color : 'blue')
  }

  function authorLabel(m) {
    const a = String(m.author || '')
    if (a === 'contact') {
      return null
    }
    if (a === 'user') {
      return 'You (board)'
    }
    if (a === 'phone') {
      return 'Phone'
    }
    if (a === 'auto_reply') {
      return 'Auto-reply'
    }
    if (a === 'cli') {
      return 'CLI'
    }
    if (a.indexOf('agent:') === 0) {
      return 'Agent ' + a.slice(6)
    }
    if (a.indexOf('rule:') === 0) {
      return 'Rule #' + a.slice(5)
    }
    return a
  }

  // Delivery ticks like WhatsApp: ✓ sent, ✓✓ delivered, coloured ✓✓ read/played. Imported history shows none.
  function tickInfo(m) {
    if (m.source === 'history') {
      return null
    }
    const sent = 'Sent ' + fmtTime(m.ts)
    if (m.status === 'pending') {
      return { text: 'Sending…', cls: '', title: 'Sending' }
    }
    if (m.status === 'sent') {
      return { text: '✓', cls: '', title: sent }
    }
    if (m.status === 'delivered' || m.status === 'read' || m.status === 'played') {
      const read = m.status !== 'delivered'
      return {
        text: '✓✓',
        cls: read ? 'wab-ticks--read' : '',
        title:
          sent +
          (m.delivered_at ? ' · Delivered ' + fmtTime(m.delivered_at) : '') +
          (read && m.read_at ? ' · ' + (m.status === 'played' ? 'Played ' : 'Read ') + fmtTime(m.read_at) : '')
      }
    }
    if (m.status === 'failed') {
      return { text: 'Failed', cls: '', title: 'Failed' }
    }
    if (m.status === 'draft') {
      return { text: 'Draft', cls: '', title: 'Draft' }
    }
    return null
  }

  function Ticks({ m }) {
    const info = tickInfo(m)
    return info ? h('span', { className: 'wab-ticks ' + info.cls, title: info.title }, info.text) : null
  }

  function colorLuminance(css) {
    let m = /^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)/.exec(css)
    if (m) {
      return (0.2126 * m[1] + 0.7152 * m[2] + 0.0722 * m[3]) / 255
    }
    m = /^color\(\s*srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)/.exec(css)
    if (m) {
      return 0.2126 * m[1] + 0.7152 * m[2] + 0.0722 * m[3]
    }
    return null
  }

  // Fetch a GET path on mount/path change/tick; keep the last data on failure.
  function useApi(path, tick) {
    const [state, setState] = useState({ data: null, error: null, status: null })
    useEffect(() => {
      if (!path) {
        return undefined
      }
      let cancelled = false
      fetchJSON(API + path).then(
        d => {
          if (!cancelled) {
            setState({ data: d, error: null, status: null })
          }
        },
        e => {
          if (!cancelled) {
            setState(s => ({ data: s.data, error: errorText(e), status: errorStatus(e) }))
          }
        }
      )
      return () => {
        cancelled = true
      }
    }, [path, tick])
    return state
  }

  // Standard rendering of "no data yet" / "stale data" for a useApi result.
  function loadNotice(api) {
    if (!api.data) {
      return api.error
        ? h(Card, null, h(CardContent, null, 'Backend unreachable: ' + api.error))
        : h('div', { className: 'wab-muted wab-pad' }, 'Loading…')
    }
    return api.error ? h('div', { className: 'wab-warn' }, 'Data may be stale: ' + api.error) : null
  }

  // --- Shared small components ----------------------------------------------

  function AccountDot({ color }) {
    return h('span', { className: dotClass(color), 'aria-hidden': 'true' })
  }

  function AccountSelect({ accounts, value, onChange }) {
    return h(
      'select',
      { className: 'wab-select', value: value || '', onChange: e => onChange(e.target.value) },
      h('option', { value: '' }, 'All numbers'),
      accounts.map(a => h('option', { key: a.id, value: String(a.id) }, a.label))
    )
  }

  function SearchForm({ placeholder, onSearch }) {
    const [draft, setDraft] = useState('')
    return h(
      'form',
      {
        className: 'wab-row wab-nowrap',
        onSubmit: e => {
          e.preventDefault()
          onSearch(draft.trim())
        }
      },
      h(Input, { value: draft, placeholder, onChange: e => setDraft(e.target.value) }),
      h(Button, { size: 'sm', type: 'submit' }, 'Search')
    )
  }

  // --- Board ---------------------------------------------------------------

  function BoardCard({ card, onOpen }) {
    const unknownState = !STATE_LABELS[card.state]
    return h(
      'div',
      {
        className: 'wab-card wab-card--' + card.urgency,
        draggable: true,
        onDragStart: e => e.dataTransfer.setData('text/plain', String(card.id)),
        onClick: () => onOpen(card.id)
      },
      h(
        'div',
        { className: 'wab-row' },
        h(AccountDot, { color: card.account_color }),
        h('strong', null, convName(card)),
        card.priority >= 2 ? h(Badge, { tone: 'destructive' }, 'Escalated') : null,
        !card.agent_active ? h(Badge, { tone: 'warning' }, 'Human') : null,
        card.has_draft ? h(Badge, { tone: 'outline' }, 'Draft') : null,
        unknownState ? h(Badge, { tone: 'outline' }, card.state) : null,
        convMarks(card)
      ),
      h('div', { className: 'wab-preview' }, card.last_message_preview),
      h(
        'div',
        { className: 'wab-row wab-muted' },
        h('span', null, card.account_label),
        h('span', null, fmtAge(card.age_seconds)),
        card.unread_count > 0 ? h(Badge, { tone: 'default' }, String(card.unread_count)) : null,
        card.state === 'muted' && card.muted_until ? h('span', null, 'until ' + fmtTime(card.muted_until)) : null
      )
    )
  }

  function BoardColumn({ col, cards, count, hidden, onOpen, onDropCard }) {
    const [over, setOver] = useState(false)
    return h(
      'div',
      {
        className: 'wab-col' + (over ? ' wab-col--over' : ''),
        onDragOver: e => {
          e.preventDefault()
          setOver(true)
        },
        onDragLeave: () => setOver(false),
        onDrop: e => {
          e.preventDefault()
          setOver(false)
          const id = Number(e.dataTransfer.getData('text/plain'))
          if (id) {
            onDropCard(id, col.name)
          }
        }
      },
      h(
        'div',
        { className: 'wab-row' },
        h('strong', null, stateLabel(col.name)),
        h(Badge, { tone: 'secondary' }, String(count))
      ),
      hidden
        ? h('div', { className: 'wab-muted wab-empty' }, 'Hidden. Use "Show closed" to list them.')
        : cards.length === 0
          ? h('div', { className: 'wab-muted wab-empty' }, 'No conversations')
          : null,
      cards.map(card => h(BoardCard, { key: card.id, card, onOpen }))
    )
  }

  function BoardView() {
    const app = useContext(AppCtx)
    const [query, setQuery] = useState('')
    const [includeClosed, setIncludeClosed] = useState(false)
    const board = useApi(
      '/board' + qs({ account_id: app.accountId, q: query, include_closed: String(includeClosed) }),
      app.tick
    )

    function onDropCard(id, columnName) {
      const cards = board.data ? board.data.columns.flatMap(c => c.cards) : []
      const card = cards.find(c => c.id === id)
      if (card && card.state === columnName) {
        return
      }
      if (columnName === 'muted') {
        const hours =
          (app.settings && app.settings.board && app.settings.board.drop_mute_hours) || DEFAULT_DROP_MUTE_HOURS
        app.call('POST', '/conversations/' + id + '/mute', { muted_until: nowSec() + hours * 3600 })
      } else {
        app.call('POST', '/conversations/' + id + '/state', { state: columnName })
      }
    }

    return h(
      'div',
      null,
      h(
        'div',
        { className: 'wab-row wab-toolbar' },
        h(SearchForm, { placeholder: 'Search name or phone', onSearch: setQuery }),
        h(
          Button,
          { size: 'sm', outlined: true, onClick: () => setIncludeClosed(v => !v) },
          includeClosed ? 'Hide closed' : 'Show closed'
        )
      ),
      loadNotice(board),
      board.data
        ? h(
            'div',
            { className: 'wab-board' },
            board.data.columns.map(col =>
              h(BoardColumn, {
                key: col.name,
                col,
                cards: col.cards,
                count:
                  col.name === 'closed' && !includeClosed ? (board.data.counts || {}).closed || 0 : col.cards.length,
                hidden: col.name === 'closed' && !includeClosed,
                onOpen: app.openConversation,
                onDropCard
              })
            )
          )
        : null
    )
  }

  // --- Chats ---------------------------------------------------------------

  function ChatListItem({ card, selected, onSelect }) {
    return h(
      'div',
      {
        className: 'wab-item' + (selected ? ' wab-item--selected' : ''),
        onClick: () => onSelect(card.id)
      },
      h(
        'div',
        { className: 'wab-row wab-nowrap' },
        h(AccountDot, { color: card.account_color }),
        h('strong', { className: 'wab-grow wab-ellipsis' }, convName(card)),
        h('span', { className: 'wab-muted wab-small' }, fmtAge(card.age_seconds)),
        card.unread_count > 0 ? h(Badge, { tone: 'default' }, String(card.unread_count)) : null
      ),
      card.is_group || card.list || card.silenced ? h('div', { className: 'wab-row' }, convMarks(card)) : null,
      h(
        'div',
        { className: 'wab-row wab-nowrap' },
        h(
          'span',
          { className: 'wab-grow wab-ellipsis wab-muted wab-small' },
          card.has_draft ? h('em', { className: 'wab-draft-mark' }, 'Draft · ') : null,
          card.last_message_direction === 'out' ? 'You: ' : '',
          card.last_message_status ? [h(Ticks, { key: 'ticks', m: { status: card.last_message_status } }), ' '] : null,
          card.last_message_preview
        ),
        h(Badge, { tone: 'outline' }, stateLabel(card.state))
      )
    )
  }

  function MediaItem({ messageId, item }) {
    const app = useContext(AppCtx)
    const [state, setState] = useState({ status: 'idle', data: null })
    const label = (item.name || item.type || 'file') + (item.size ? ' (' + fmtSize(item.size) + ')' : '')
    const isImage = mime => /^image\//.test(mime || '')

    function load() {
      setState({ status: 'loading', data: null })
      fetchJSON(API + '/messages/' + messageId + '/media/' + item.index).then(
        d => setState({ status: 'ready', data: d }),
        e => {
          setState({ status: 'idle', data: null })
          app.showToast(failureText(e), 'error')
        }
      )
    }

    if (!item.available) {
      return h('div', { className: 'wab-muted wab-small' }, label + ' — unavailable')
    }
    if (state.status === 'ready') {
      const d = state.data
      return isImage(d.mime || item.mime)
        ? h('img', { className: 'wab-media', src: d.data_url, alt: d.name || label })
        : h(
            'a',
            { className: 'wab-link', href: d.data_url, download: d.name || item.name || 'file' },
            'Download ' + label
          )
    }
    return h(
      Button,
      { size: 'sm', outlined: true, disabled: state.status === 'loading', onClick: load },
      state.status === 'loading' ? 'Loading…' : (isImage(item.mime) ? 'Show image: ' : 'Load file: ') + label
    )
  }

  function MessageBubble({ m, canSend, sender }) {
    const app = useContext(AppCtx)
    const who = authorLabel(m)
    const media = m.media || []
    const failed = m.status === 'failed'
    return h(
      'div',
      {
        className:
          'wab-msg wab-msg--' +
          m.direction +
          (failed ? ' wab-msg--failed' : '') +
          (m.status === 'pending' ? ' wab-msg--pending' : '')
      },
      who ? h('div', { className: 'wab-msg-author' }, who) : null,
      sender
        ? h(
            'div',
            { className: 'wab-msg-author', title: sender.title, style: { color: sender.color, fontWeight: 600 } },
            sender.label
          )
        : null,
      media.map(item => h(MediaItem, { key: item.index, messageId: m.id, item })),
      m.body ? h('div', { className: 'wab-msg-body' }, m.body) : null,
      h(
        'div',
        { className: 'wab-msg-meta wab-muted' },
        fmtTime(m.ts),
        m.source === 'history' ? ' · imported' : '',
        m.direction === 'out' ? [' · ', h(Ticks, { key: 'ticks', m })] : null
      ),
      failed && m.error ? h('div', { className: 'wab-error wab-small' }, m.error) : null,
      failed && m.body && canSend
        ? h(
            Button,
            {
              size: 'sm',
              outlined: true,
              onClick: () => app.call('POST', '/conversations/' + m.conversation_id + '/reply', { text: m.body })
            },
            'Resend'
          )
        : null
    )
  }

  function DraftBubble({ m, canSend, reason }) {
    const app = useContext(AppCtx)
    const [text, setText] = useState(m.body || '')
    const [busy, setBusy] = useState(false)
    const who = authorLabel(m)

    function approve() {
      setBusy(true)
      const body = text !== (m.body || '') ? { text } : {}
      app.call('POST', '/messages/' + m.id + '/approve', body).finally(() => setBusy(false))
    }
    function discard() {
      setBusy(true)
      app.call('POST', '/messages/' + m.id + '/discard', {}).finally(() => setBusy(false))
    }

    return h(
      'div',
      { className: 'wab-msg wab-msg--out wab-msg--draft' },
      h('div', { className: 'wab-msg-author' }, 'Draft' + (who ? ' · ' + who : '')),
      h('textarea', {
        className: 'wab-reply',
        rows: 3,
        value: text,
        disabled: busy,
        onChange: e => setText(e.target.value)
      }),
      h(
        'div',
        { className: 'wab-row' },
        h(Button, { size: 'sm', disabled: busy || !canSend || !text.trim(), onClick: approve }, 'Approve & send'),
        h(Button, { size: 'sm', outlined: true, disabled: busy, onClick: discard }, 'Discard'),
        h('span', { className: 'wab-muted wab-small' }, fmtTime(m.ts))
      ),
      !canSend && reason ? h('div', { className: 'wab-muted wab-small' }, reason) : null
    )
  }

  function Composer({ canSend, reason, onSend }) {
    const [text, setText] = useState('')
    const [sending, setSending] = useState(false)

    function send() {
      const value = text.trim()
      if (!canSend || sending || !value) {
        return
      }
      setSending(true)
      onSend(value)
        .then(ok => ok && setText(''))
        .finally(() => setSending(false))
    }

    return h(
      'form',
      {
        className: 'wab-composer',
        onSubmit: e => {
          e.preventDefault()
          send()
        }
      },
      h('textarea', {
        className: 'wab-reply',
        rows: 3,
        placeholder: canSend ? 'Write a reply… (Enter to send, Shift+Enter for a new line)' : reason,
        value: text,
        disabled: !canSend || sending,
        onChange: e => setText(e.target.value),
        onKeyDown: e => {
          if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault()
            send()
          }
        }
      }),
      h(
        'div',
        { className: 'wab-row' },
        h(
          Button,
          { size: 'sm', type: 'submit', disabled: !canSend || sending || !text.trim() },
          sending ? 'Sending…' : 'Send'
        ),
        !canSend && reason ? h('span', { className: 'wab-muted wab-small' }, reason) : null
      )
    )
  }

  function sendBlockReason(app, account, jid) {
    if (!app.serviceKnown || !app.serviceUp) {
      return 'WhatsApp service is not running'
    }
    if (!account) {
      return 'Loading…'
    }
    if (account.kind === 'demo') {
      return 'Demo chat: replies are disabled'
    }
    if (!WA_JID_RE.test(jid)) {
      return 'Replies are only possible to WhatsApp chats'
    }
    if (account.desired !== 'running') {
      return 'Number "' + account.label + '" is stopped'
    }
    const st = account.status
    if (!st || st.state !== 'connected') {
      return (
        'Number "' +
        account.label +
        '" is not connected' +
        (st ? ' (' + (ACCOUNT_STATE_LABELS[st.state] || st.state) + ')' : '')
      )
    }
    if (!st.heartbeat_at || nowSec() - st.heartbeat_at > SERVICE_FRESH_SECONDS) {
      return 'WhatsApp service is not running'
    }
    return ''
  }

  function Thread({ id }) {
    const app = useContext(AppCtx)
    const base = '/conversations/' + id
    const detail = useApi(base, app.tick)
    const latest = useApi(base + '/messages?limit=50', app.tick)
    const [older, setOlder] = useState([])
    const [olderMore, setOlderMore] = useState(null)
    const [loadingOlder, setLoadingOlder] = useState(false)
    const listRef = useRef(null)
    const stickRef = useRef(true)
    const restoreRef = useRef(null)

    const messages = useMemo(() => {
      const byId = new Map()
      older.forEach(m => byId.set(m.id, m))
      if (latest.data) {
        latest.data.messages.forEach(m => byId.set(m.id, m))
      }
      return Array.from(byId.values())
        .filter(m => m.status !== 'discarded')
        .sort((a, b) => a.ts - b.ts || a.id - b.id)
    }, [older, latest.data])
    const hasMore = olderMore !== null ? olderMore : latest.data ? !!latest.data.has_more : false

    useEffect(() => {
      const el = listRef.current
      if (!el) {
        return
      }
      if (restoreRef.current !== null) {
        el.scrollTop = el.scrollHeight - restoreRef.current
        restoreRef.current = null
      } else if (stickRef.current) {
        el.scrollTop = el.scrollHeight
      }
    }, [messages])

    const unread = detail.data ? detail.data.conversation.unread_count : 0
    useEffect(() => {
      if (unread > 0) {
        app.callQuiet('POST', base + '/read', {})
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [id, unread])

    function onScroll() {
      const el = listRef.current
      stickRef.current = !el || el.scrollHeight - el.scrollTop - el.clientHeight < 80
    }

    function loadOlder() {
      if (!messages.length || loadingOlder) {
        return
      }
      const el = listRef.current
      restoreRef.current = el ? el.scrollHeight - el.scrollTop : null
      const minId = messages.reduce((acc, m) => Math.min(acc, m.id), messages[0].id)
      setLoadingOlder(true)
      fetchJSON(API + base + '/messages' + qs({ before_id: minId, limit: 50 })).then(
        res => {
          setOlder(prev => res.messages.concat(prev))
          setOlderMore(!!res.has_more)
          setLoadingOlder(false)
        },
        e => {
          restoreRef.current = null
          setLoadingOlder(false)
          app.showToast(failureText(e), 'error')
        }
      )
    }

    const notice = loadNotice(detail)
    if (!detail.data) {
      return h('div', { className: 'wab-thread-pane' }, notice)
    }

    const d = detail.data
    const conv = d.conversation
    const account = d.account
    const reason = sendBlockReason(app, account, conv.chat_jid)
    const canSend = !reason
    const allowed = d.allowed_states || []
    const presets =
      (app.settings && app.settings.board && app.settings.board.mute_presets_hours) || DEFAULT_MUTE_PRESETS

    const button = (label, path, body) =>
      h(Button, { key: label, size: 'sm', outlined: true, onClick: () => app.call('POST', base + path, body) }, label)
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
      actions.push(
        h(
          'select',
          {
            key: 'mute',
            className: 'wab-select',
            value: '',
            onChange: e => {
              const hours = Number(e.target.value)
              if (hours) {
                app.call('POST', base + '/mute', { muted_until: nowSec() + hours * 3600 })
              }
            }
          },
          h('option', { value: '' }, 'Mute…'),
          presets.map(hrs => h('option', { key: hrs, value: String(hrs) }, fmtHours(hrs)))
        )
      )
    }

    // Group threads name the sender once per run of consecutive messages from the same person.
    let lastSender = ''
    const bubbles = messages.map(m => {
      if (m.status === 'draft') {
        lastSender = ''
        return h(DraftBubble, { key: m.id, m, canSend, reason })
      }
      let sender = null
      if (conv.is_group && m.direction === 'in') {
        const senderKey = m.sender_jid || m.sender_name || '?'
        if (senderKey !== lastSender) {
          sender = { label: senderLabel(m), title: senderPhone(m), color: senderColor(senderKey) }
        }
        lastSender = senderKey
      } else {
        lastSender = ''
      }
      return h(MessageBubble, { key: m.id, m, canSend, sender })
    })

    return h(
      'div',
      { className: 'wab-thread-pane' },
      notice,
      h(
        'div',
        { className: 'wab-thread-head' },
        h(
          'div',
          { className: 'wab-row' },
          h(AccountDot, { color: conv.account_color }),
          h('strong', null, convName(conv)),
          h(Badge, { tone: 'outline' }, stateLabel(conv.state)),
          convMarks(conv),
          conv.priority >= 2 ? h(Badge, { tone: 'destructive' }, 'Escalated') : null,
          !conv.agent_active ? h(Badge, { tone: 'warning' }, 'Human') : null,
          conv.state === 'muted' && conv.muted_until
            ? h('span', { className: 'wab-muted wab-small' }, 'until ' + fmtTime(conv.muted_until))
            : null
        ),
        h(
          'div',
          { className: 'wab-row wab-muted wab-small' },
          h('span', null, conv.account_label),
          conv.is_group
            ? h('span', null, 'Group · ' + (d.participants || conv.participants || []).length + ' participants')
            : conv.phone
              ? h('span', null, '+' + String(conv.phone).replace(/^\+/, ''))
              : null,
          (conv.tags || []).map(t => h(Badge, { key: t, tone: 'secondary' }, t))
        ),
        h('div', { className: 'wab-actions' }, actions),
        d.state_history && d.state_history.length
          ? h(
              'details',
              { className: 'wab-small' },
              h('summary', { className: 'wab-muted' }, 'State history'),
              d.state_history.map(r =>
                h(
                  'div',
                  { key: r.id, className: 'wab-muted' },
                  (r.from_state ? stateLabel(r.from_state) : '—') +
                    ' → ' +
                    stateLabel(r.to_state) +
                    ' · ' +
                    [r.actor, r.reason, fmtTime(r.at)].filter(Boolean).join(' · ')
                )
              )
            )
          : null
      ),
      h(
        'div',
        { className: 'wab-messages', ref: listRef, onScroll },
        hasMore
          ? h(
              'div',
              { className: 'wab-center' },
              h(
                Button,
                { size: 'sm', outlined: true, disabled: loadingOlder, onClick: loadOlder },
                loadingOlder ? 'Loading…' : 'Load older'
              )
            )
          : null,
        messages.length === 0 && latest.data
          ? h('div', { className: 'wab-muted wab-center' }, 'No messages yet')
          : null,
        bubbles
      ),
      h(Composer, {
        canSend,
        reason,
        onSend: text => app.call('POST', base + '/reply', { text }).then(res => res !== null)
      })
    )
  }

  function ChatsView() {
    const app = useContext(AppCtx)
    const [query, setQuery] = useState('')
    const [unreadOnly, setUnreadOnly] = useState(false)
    const [limit, setLimit] = useState(50)
    const list = useApi(
      '/conversations' +
        qs({ account_id: app.accountId, q: query, unread_only: unreadOnly ? 'true' : '', limit, offset: 0 }),
      app.tick
    )
    const items = list.data ? list.data.conversations : []
    const total = list.data ? list.data.total : 0

    useEffect(() => {
      setLimit(50)
    }, [app.accountId])

    return h(
      'div',
      { className: 'wab-chats' },
      h(
        'div',
        { className: 'wab-list' },
        h(
          'div',
          { className: 'wab-list-head' },
          h(SearchForm, {
            placeholder: 'Search name or phone',
            onSearch: v => {
              setQuery(v)
              setLimit(50)
            }
          }),
          h(
            Button,
            {
              size: 'sm',
              outlined: !unreadOnly,
              onClick: () => {
                setUnreadOnly(v => !v)
                setLimit(50)
              }
            },
            unreadOnly ? 'Showing unread' : 'Unread only'
          )
        ),
        loadNotice(list),
        h(
          'div',
          { className: 'wab-list-body' },
          list.data && items.length === 0 ? h('div', { className: 'wab-muted wab-pad' }, 'No conversations') : null,
          items.map(card =>
            h(ChatListItem, { key: card.id, card, selected: card.id === app.selectedId, onSelect: app.setSelectedId })
          ),
          list.data && total > items.length
            ? limit < MAX_LIST_LIMIT
              ? h(
                  'div',
                  { className: 'wab-center wab-pad' },
                  h(
                    Button,
                    { size: 'sm', outlined: true, onClick: () => setLimit(l => Math.min(l + 50, MAX_LIST_LIMIT)) },
                    'Show more (' + (total - items.length) + ')'
                  )
                )
              : h(
                  'div',
                  { className: 'wab-muted wab-center wab-pad wab-small' },
                  'Refine the search to see older chats'
                )
            : null
        )
      ),
      app.selectedId
        ? h(Thread, { key: app.selectedId, id: app.selectedId })
        : h('div', { className: 'wab-thread-pane wab-muted wab-center wab-pad' }, 'Select a conversation')
    )
  }

  // --- Numbers -------------------------------------------------------------

  function QrBox({ svg }) {
    const ref = useRef(null)
    // The SVG paints with currentColor. WhatsApp needs dark modules on a light
    // background, so flip the box when the active theme is light-on-dark.
    useEffect(() => {
      const el = ref.current
      if (!el) {
        return
      }
      const style = window.getComputedStyle(el)
      const fg = colorLuminance(style.color)
      const bg = colorLuminance(style.backgroundColor)
      el.style.filter = fg !== null && bg !== null && fg > bg ? 'invert(1)' : ''
    }, [svg])
    return h('div', { className: 'wab-qr', ref, dangerouslySetInnerHTML: { __html: svg } })
  }

  function accountStateInfo(a, serviceUp) {
    if (a.kind === 'demo') {
      return ['Demo', 'secondary']
    }
    if (!serviceUp) {
      return ['Service not running', 'destructive']
    }
    if (!a.status) {
      return ['Waiting for service…', 'warning']
    }
    return [ACCOUNT_STATE_LABELS[a.status.state] || a.status.state, ACCOUNT_STATE_TONES[a.status.state] || 'outline']
  }

  function AccountCard({ a }) {
    const app = useContext(AppCtx)
    const [stateText, stateTone] = accountStateInfo(a, app.serviceUp)
    const st = a.status
    const running = a.desired === 'running'
    const path = '/accounts/' + a.id
    const patch = fields => app.call('PATCH', path, fields)

    function remove() {
      if (!window.confirm('Remove number "' + a.label + '"? Its WhatsApp session will be deleted.')) {
        return
      }
      const del = window.confirm(
        "Also delete this number's conversations and messages from the board?\n\nOK = delete them, Cancel = keep them."
      )
      app.call('DELETE', path + qs({ delete_conversations: del ? 'true' : 'false' }))
    }
    function logout() {
      if (
        window.confirm(
          'Log out "' + a.label + '"? The linked device session is deleted and you will need to scan a QR code again.'
        )
      ) {
        app.call('POST', path + '/logout', {})
      }
    }
    function rename() {
      const label = window.prompt('Number name', a.label)
      if (label && label.trim() && label.trim() !== a.label) {
        patch({ label: label.trim() })
      }
    }

    const isWa = a.kind === 'whatsapp'
    const showQr = isWa && app.serviceUp && running && st && st.state === 'qr' && st.qr_svg
    const waiting =
      isWa &&
      app.serviceUp &&
      running &&
      st &&
      (st.state === 'starting' || st.state === 'pairing' || (st.state === 'qr' && !st.qr_svg))

    return h(
      Card,
      null,
      h(
        CardContent,
        { className: 'wab-account' },
        h(
          'div',
          { className: 'wab-row' },
          h(AccountDot, { color: a.color }),
          h('strong', null, a.label),
          h(Badge, { tone: stateTone }, stateText),
          a.kind === 'demo'
            ? null
            : h(Badge, { tone: 'outline' }, a.desired === 'running' ? 'Enabled' : a.desired.replace('_', ' '))
        ),
        h(
          'div',
          { className: 'wab-muted wab-small' },
          [
            a.phone
              ? '+' + String(a.phone).replace(/^\+/, '')
              : isWa
                ? a.paired
                  ? 'Paired'
                  : 'Not paired yet'
                : 'Sample conversations',
            a.wa_name,
            st && st.updated_at ? 'updated ' + fmtAge(nowSec() - st.updated_at) + ' ago' : ''
          ]
            .filter(Boolean)
            .join(' · ')
        ),
        st && st.error && st.state !== 'connected' ? h('div', { className: 'wab-error wab-small' }, st.error) : null,
        showQr
          ? h(
              'div',
              { className: 'wab-pairing' },
              h(QrBox, { svg: st.qr_svg }),
              h(
                'div',
                { className: 'wab-small' },
                h('strong', null, 'Link this number'),
                h('div', null, '1. Open WhatsApp on the phone with this number.'),
                h('div', null, '2. Go to Settings → Linked devices → Link a device.'),
                h('div', null, '3. Scan this QR code. It refreshes automatically.'),
                st.qr_at
                  ? h('div', { className: 'wab-muted' }, 'Generated ' + fmtAge(nowSec() - st.qr_at) + ' ago')
                  : null
              )
            )
          : null,
        waiting ? h('div', { className: 'wab-muted wab-small' }, 'Preparing the pairing QR code…') : null,
        h(
          'div',
          { className: 'wab-actions' },
          isWa && !running
            ? h(
                Button,
                { size: 'sm', onClick: () => app.call('POST', path + '/start', {}) },
                a.paired ? 'Start' : 'Start pairing'
              )
            : null,
          isWa && running
            ? h(Button, { size: 'sm', outlined: true, onClick: () => app.call('POST', path + '/stop', {}) }, 'Stop')
            : null,
          isWa && running
            ? h(
                Button,
                { size: 'sm', outlined: true, onClick: () => app.call('POST', path + '/restart', {}) },
                'Restart'
              )
            : null,
          isWa && a.paired ? h(Button, { size: 'sm', outlined: true, onClick: logout }, 'Log out') : null,
          h(Button, { size: 'sm', outlined: true, destructive: true, onClick: remove }, 'Remove'),
          h(Button, { size: 'sm', ghost: true, onClick: rename }, 'Rename'),
          h(
            'select',
            {
              className: 'wab-select',
              value: a.color,
              title: 'Color',
              onChange: e => patch({ color: e.target.value })
            },
            (ACCOUNT_COLORS.indexOf(a.color) === -1 ? [a.color] : [])
              .concat(ACCOUNT_COLORS)
              .map(c => h('option', { key: c, value: c }, c))
          ),
          isWa
            ? h(
                'select',
                {
                  className: 'wab-select',
                  value: a.history_mode,
                  title: 'History import (applies when the number is next linked or restarted)',
                  onChange: e => patch({ history_mode: e.target.value })
                },
                HISTORY_MODES.map(([value, label]) => h('option', { key: value, value }, label))
              )
            : null
        )
      )
    )
  }

  function AddNumberForm() {
    const app = useContext(AppCtx)
    const [label, setLabel] = useState('')
    const [color, setColor] = useState('blue')
    const [mode, setMode] = useState('recent')
    const [busy, setBusy] = useState(false)

    function submit(e) {
      e.preventDefault()
      if (!label.trim()) {
        return
      }
      setBusy(true)
      app
        .call('POST', '/accounts', { label: label.trim(), color, history_mode: mode })
        .then(res => {
          if (res) {
            setLabel('')
            app.showToast('Number added. Scan the QR code when it appears.', 'success')
          }
        })
        .finally(() => setBusy(false))
    }

    return h(
      Card,
      null,
      h(
        CardContent,
        null,
        h('strong', null, 'Add number'),
        h(
          'form',
          { className: 'wab-row wab-addform', onSubmit: submit },
          h(Input, { value: label, placeholder: 'Label (e.g. Support)', onChange: e => setLabel(e.target.value) }),
          h(
            'select',
            { className: 'wab-select', value: color, title: 'Color', onChange: e => setColor(e.target.value) },
            ACCOUNT_COLORS.map(c => h('option', { key: c, value: c }, c))
          ),
          h(
            'select',
            { className: 'wab-select', value: mode, title: 'History import', onChange: e => setMode(e.target.value) },
            HISTORY_MODES.map(([value, text]) => h('option', { key: value, value }, text))
          ),
          h(Button, { size: 'sm', type: 'submit', disabled: busy || !label.trim() }, busy ? 'Adding…' : 'Add number')
        )
      )
    )
  }

  const SERVICE_LOG_HINT = '~/.hermes/plugin-data/hermes-whatsapp-chat/logs/channel.log'

  // POSTs a /service/* or /skill/* route; app.call toasts failures and refreshes.
  // After 'install' it keeps `busy` set until the restarted service reports a fresh heartbeat.
  function useServiceAction() {
    const app = useContext(AppCtx)
    const [busy, setBusy] = useState('')
    function run(key, path, doneMessage) {
      setBusy(key)
      const since = nowSec()
      return app.call('POST', path, {}).then(res => {
        if (!res || key !== 'install') {
          setBusy('')
          if (res && doneMessage) {
            app.showToast(doneMessage, 'success')
          }
          return res
        }
        app.setRestart('restarting')
        return app.waitForHeartbeat(since).then(started => {
          app.setRestart(started ? '' : 'failed')
          app.refresh()
          setBusy('')
          if (started) {
            if (doneMessage) {
              app.showToast(doneMessage, 'success')
            }
          } else {
            app.showToast(
              'Service did not start: no new heartbeat after 30 s. Check the log: ' + SERVICE_LOG_HINT,
              'error'
            )
          }
          return res
        })
      })
    }
    return [busy, run]
  }

  function RestartBanner() {
    return h(
      Card,
      { className: 'wab-banner' },
      h(
        CardContent,
        null,
        h('strong', { className: 'wab-error' }, RESTART_TITLE),
        h('div', { className: 'wab-small' }, 'The plugin was updated but Hermes is still running the previous backend.')
      )
    )
  }

  function ServiceBanner() {
    const app = useContext(AppCtx)
    const [busy, run] = useServiceAction()
    const installed = !app.service || app.service.installed !== false
    const restarting = app.restart === 'restarting'
    return h(
      Card,
      { className: 'wab-banner' },
      h(
        CardContent,
        null,
        h(
          'strong',
          { className: 'wab-error' },
          restarting
            ? 'Restarting service…'
            : installed
              ? 'WhatsApp service is not running'
              : 'WhatsApp service is not installed'
        ),
        h(
          'div',
          { className: 'wab-small' },
          restarting
            ? 'Waiting for the service to report in. This can take up to 30 seconds.'
            : installed
              ? 'Numbers cannot connect, receive or send messages until the background service is running. Reinstalling restarts it.'
              : 'Numbers cannot connect, receive or send messages until the background service is installed. It starts automatically and keeps running in the background.'
        ),
        app.restart === 'failed'
          ? h(
              'div',
              { className: 'wab-error wab-small' },
              'The service did not start within 30 s. Check the log: ',
              h('code', { className: 'wab-code' }, SERVICE_LOG_HINT)
            )
          : installed && !restarting
            ? h(
                'div',
                { className: 'wab-muted wab-small' },
                'If it keeps failing, check the log: ',
                h('code', { className: 'wab-code' }, SERVICE_LOG_HINT)
              )
            : null,
        app.outdated ? h('div', { className: 'wab-warn wab-small' }, RESTART_TITLE) : null,
        h(
          'div',
          { className: 'wab-actions' },
          h(
            Button,
            {
              size: 'sm',
              disabled: busy !== '' || restarting || app.outdated,
              title: app.outdated ? RESTART_TITLE : undefined,
              onClick: () => run('install', '/service/install', 'WhatsApp service installed')
            },
            busy === 'install' || restarting ? 'Restarting service…' : installed ? 'Reinstall' : 'Install service'
          )
        )
      )
    )
  }

  function ServiceCard() {
    const app = useContext(AppCtx)
    const [busy, run] = useServiceAction()
    const s = app.service
    if (!s) {
      return null
    }
    const installed = s.installed !== false
    const restarting = app.restart === 'restarting'
    const locked = busy !== '' || restarting || app.outdated
    const lockTitle = app.outdated ? RESTART_TITLE : undefined
    const nodeKnown = Object.prototype.hasOwnProperty.call(s, 'node')
    function uninstall() {
      if (
        window.confirm(
          'Uninstall the WhatsApp service? Numbers stay linked but stop receiving and sending until it is installed again.'
        )
      ) {
        run('uninstall', '/service/uninstall', 'WhatsApp service uninstalled')
      }
    }
    return h(
      Card,
      null,
      h(
        CardContent,
        { className: 'wab-account' },
        h(
          'div',
          { className: 'wab-row' },
          h('strong', null, 'Service'),
          h(
            Badge,
            { tone: s.running ? 'success' : installed ? 'destructive' : 'secondary' },
            s.running ? 'Running' : installed ? 'Not running' : 'Not installed'
          )
        ),
        h(
          'div',
          { className: 'wab-muted wab-small' },
          'The background process that keeps your numbers connected and delivers messages. It starts automatically and restarts if it stops.'
        ),
        h(
          'div',
          { className: 'wab-muted wab-small' },
          [
            installed ? 'Installed' : 'Not installed',
            s.running && s.pid ? 'PID ' + s.pid : '',
            s.version ? 'version ' + s.version : '',
            s.heartbeat_at ? 'heartbeat ' + fmtAge(nowSec() - s.heartbeat_at) + ' ago' : ''
          ]
            .filter(Boolean)
            .join(' · ')
        ),
        h(
          'div',
          { className: s.node ? 'wab-muted wab-small' : nodeKnown ? 'wab-error wab-small' : 'wab-warn wab-small' },
          s.node
            ? 'Node.js: ' + s.node
            : nodeKnown
              ? 'Node.js not found. Install Node.js, then install the service.'
              : 'Node.js: unknown (restart Hermes)'
        ),
        restarting
          ? h('div', { className: 'wab-small' }, 'Restarting service… waiting for it to report in (up to 30 s).')
          : null,
        app.restart === 'failed' && !s.running
          ? h(
              'div',
              { className: 'wab-error wab-small' },
              'The service did not start within 30 s. Check the log: ',
              h('code', { className: 'wab-code' }, SERVICE_LOG_HINT)
            )
          : null,
        installed && !s.running && !restarting && app.restart !== 'failed'
          ? h(
              'div',
              { className: 'wab-muted wab-small' },
              'Not running. Try Reinstall; details are written to ',
              h('code', { className: 'wab-code' }, SERVICE_LOG_HINT)
            )
          : null,
        app.outdated ? h('div', { className: 'wab-warn wab-small' }, RESTART_TITLE) : null,
        h(
          'div',
          { className: 'wab-actions' },
          h(
            Button,
            {
              size: 'sm',
              outlined: installed,
              disabled: locked,
              title: lockTitle,
              onClick: () => run('install', '/service/install', 'WhatsApp service installed')
            },
            busy === 'install' || restarting ? 'Restarting service…' : installed ? 'Reinstall' : 'Install service'
          ),
          installed
            ? h(
                Button,
                {
                  size: 'sm',
                  outlined: true,
                  destructive: true,
                  disabled: locked,
                  title: lockTitle,
                  onClick: uninstall
                },
                busy === 'uninstall' ? 'Uninstalling…' : 'Uninstall service'
              )
            : null
        )
      )
    )
  }

  function SkillCard() {
    const app = useContext(AppCtx)
    const [busy, run] = useServiceAction()
    const s = app.service
    if (!s) {
      return null
    }
    const installed = !!s.skill_installed
    const locked = busy !== '' || app.outdated
    const lockTitle = app.outdated ? RESTART_TITLE : undefined
    return h(
      Card,
      null,
      h(
        CardContent,
        { className: 'wab-account' },
        h(
          'div',
          { className: 'wab-row' },
          h('strong', null, 'Hermes skill'),
          h(Badge, { tone: installed ? 'success' : 'secondary' }, installed ? 'Installed' : 'Not installed')
        ),
        h(
          'div',
          { className: 'wab-muted wab-small' },
          'Lets Hermes agents list, read and answer your WhatsApp conversations. Installed into the Hermes skills folder.'
        ),
        app.outdated ? h('div', { className: 'wab-warn wab-small' }, RESTART_TITLE) : null,
        h(
          'div',
          { className: 'wab-actions' },
          h(
            Button,
            {
              size: 'sm',
              outlined: installed,
              disabled: locked,
              title: lockTitle,
              onClick: () => run('skill-install', '/skill/install', 'Hermes skill installed')
            },
            busy === 'skill-install' ? 'Installing…' : installed ? 'Reinstall skill' : 'Install skill'
          ),
          installed
            ? h(
                Button,
                {
                  size: 'sm',
                  outlined: true,
                  destructive: true,
                  disabled: locked,
                  title: lockTitle,
                  onClick: () => run('skill-remove', '/skill/uninstall', 'Hermes skill removed')
                },
                busy === 'skill-remove' ? 'Removing…' : 'Remove skill'
              )
            : null
        )
      )
    )
  }

  function NumbersView() {
    const app = useContext(AppCtx)
    return h(
      'div',
      { className: 'wab-numbers' },
      !app.serviceUp ? h(ServiceBanner) : null,
      h(AddNumberForm),
      app.accounts.length === 0
        ? h(
            'div',
            { className: 'wab-muted wab-pad' },
            'No numbers yet. Add one above and scan the QR code with your phone.'
          )
        : app.accounts.map(a => h(AccountCard, { key: a.id, a })),
      h(ServiceCard),
      h(SkillCard),
      h(
        'div',
        { className: 'wab-muted wab-small wab-pad' },
        'Rules, notifications and automations are configured in the Hermes desktop app.'
      )
    )
  }

  // --- App -----------------------------------------------------------------

  function App() {
    const [tab, setTab] = useState('board')
    const [selectedId, setSelectedId] = useState(null)
    const [accountId, setAccountId] = useState(() => new URLSearchParams(window.location.search).get('account') || '')
    const [tick, setTick] = useState(0)
    const { showToast, toast } = useToast()
    const accounts = useApi('/accounts', tick)
    const service = useApi('/service', tick)
    const settings = useApi('/settings', tick)
    const health = useApi('/health', tick)
    const [restart, setRestart] = useState('')
    const alive = useRef(true)

    useEffect(
      () => () => {
        alive.current = false
      },
      []
    )

    // Keep the selected number in ?account=<id> (the host manages its own params such as `profile`).
    useEffect(() => {
      const params = new URLSearchParams(window.location.search)
      if (accountId) {
        params.set('account', accountId)
      } else {
        params.delete('account')
      }
      const query = params.toString()
      if (query !== window.location.search.replace(/^\?/, '')) {
        window.history.replaceState(
          window.history.state,
          '',
          window.location.pathname + (query ? '?' + query : '') + window.location.hash
        )
      }
    }, [accountId])

    // Forget a selected number that no longer exists.
    useEffect(() => {
      if (accounts.data && accountId) {
        const known = (accounts.data.accounts || []).some(a => String(a.id) === String(accountId))
        if (!known) {
          setAccountId('')
        }
      }
    }, [accounts.data, accountId])

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

    // Mutating call: toast on failure, always refresh. Resolves to the response or null.
    function call(method, path, body) {
      return request(method, path, body)
        .then(
          res => res || {},
          e => {
            showToast(failureText(e), 'error')
            return null
          }
        )
        .finally(() => setTick(t => t + 1))
    }
    function callQuiet(method, path, body) {
      return request(method, path, body).then(
        () => setTick(t => t + 1),
        () => undefined
      )
    }

    // Resolves true once /service reports a heartbeat newer than `since` (unix seconds); false after 30 s.
    function waitForHeartbeat(since) {
      const deadline = Date.now() + SERVICE_START_TIMEOUT_MS
      function poll() {
        if (!alive.current || Date.now() >= deadline) {
          return Promise.resolve(false)
        }
        return new Promise(resolve => setTimeout(resolve, 1000))
          .then(() => fetchJSON(API + '/service'))
          .then(
            s => !!s && s.heartbeat_at > since,
            () => false
          )
          .then(ok => ok || poll())
      }
      return poll()
    }
    function openConversation(id) {
      setSelectedId(id)
      setTab('chats')
    }

    const accountList = accounts.data ? accounts.data.accounts || [] : []
    const serviceKnown = !!service.data
    const serviceUp = serviceKnown && !!service.data.running
    const outdated = health.data
      ? typeof health.data.api_version !== 'number' || health.data.api_version < REQUIRED_API_VERSION
      : health.status === 404
    const ctx = {
      tick,
      accounts: accountList,
      service: service.data,
      serviceKnown,
      outdated,
      restart,
      setRestart,
      refresh: () => setTick(t => t + 1),
      waitForHeartbeat,
      serviceUp,
      settings: settings.data,
      accountId,
      setAccountId,
      selectedId,
      setSelectedId,
      openConversation,
      call,
      callQuiet,
      showToast
    }

    if (!accounts.data) {
      return h(
        'div',
        null,
        outdated ? h(RestartBanner) : null,
        accounts.error
          ? h(Card, null, h(CardContent, null, 'Backend unreachable: ' + accounts.error))
          : h('div', { className: 'wab-muted wab-pad' }, 'Loading…')
      )
    }

    return h(
      AppCtx.Provider,
      { value: ctx },
      h(
        'div',
        { className: 'wab-root' },
        h(
          'div',
          { className: 'wab-row wab-tabs' },
          TABS.map(([id, label]) =>
            h(Button, { key: id, size: 'sm', ghost: tab !== id, onClick: () => setTab(id) }, label)
          ),
          tab !== 'numbers'
            ? h(AccountSelect, { accounts: accountList, value: accountId, onChange: setAccountId })
            : null,
          serviceKnown
            ? h(
                Badge,
                { tone: serviceUp ? 'success' : 'destructive' },
                serviceUp ? 'Service running' : 'Service not running'
              )
            : null
        ),
        outdated ? h(RestartBanner) : null,
        accounts.error ? h('div', { className: 'wab-warn' }, 'Data may be stale: ' + accounts.error) : null,
        tab !== 'numbers' && serviceKnown && !serviceUp
          ? h(
              'div',
              { className: 'wab-warn' },
              'The WhatsApp service is not running; messages cannot be sent or received. See the Numbers tab.'
            )
          : null,
        tab === 'board' ? h(BoardView) : null,
        tab === 'chats' ? h(ChatsView) : null,
        tab === 'numbers' ? h(NumbersView) : null,
        h(Toast, { toast })
      )
    )
  }

  window.__HERMES_PLUGINS__.register('hermes-whatsapp-chat', App)
})()
