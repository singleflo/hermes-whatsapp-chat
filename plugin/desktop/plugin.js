/**
 * hermes-whatsapp-chat — desktop plugin (ESM, loaded uncompiled).
 *
 * Rules: jsx() calls only (no JSX syntax). Only these imports resolve:
 * @hermes/plugin-sdk, react, react/jsx-runtime. Colors via --ui-* vars only.
 * Hot-reloads on save; if not: ⌘K → "Reload desktop plugins".
 */

import {
  Badge,
  Button,
  ErrorState,
  haptic,
  host,
  Input,
  PALETTE_AREA,
  queryClient,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  Skeleton,
  STATUSBAR_AREAS,
  StatusDot,
  Textarea,
  useQuery
} from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'hermes-whatsapp-chat'
const ROUTE = '/wa-board'
const POLL_MS = 5000

const STATE_LABELS = { new: 'New', in_progress: 'In progress', waiting: 'Waiting', muted: 'Muted', closed: 'Closed' }
const CHANNEL_LABELS = {
  connected: ['WhatsApp connected', 'success'],
  disconnected: ['WhatsApp disconnected', 'warn'],
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
const URGENCY_COLOR = { high: 'var(--ui-red)', medium: 'var(--ui-orange)', normal: 'var(--ui-stroke-secondary)' }

// Set by register(); handlers read it imperatively, never from render closures.
let ctxRef = null

// --- Helpers ---------------------------------------------------------------

const stateLabel = s => STATE_LABELS[s] || s
const nowSec = () => Math.floor(Date.now() / 1000)
const chatPath = jid => '/chats/' + encodeURIComponent(jid)
const refresh = () => queryClient.invalidateQueries({ queryKey: [ID] })

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

// ctx.rest errors read "409: {"detail": ...}".
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

function act(path, body) {
  return ctxRef
    .rest(path, { method: 'POST', body })
    .then(
      () => true,
      err => {
        host.notify({ kind: 'error', title: 'Action rejected', message: errorText(err) })
        return false
      }
    )
    .finally(refresh)
}

const secondary = { color: 'var(--ui-text-tertiary)' }

// --- Components ------------------------------------------------------------

function CardView({ card, onSelect }) {
  return jsxs('div', {
    className: 'flex cursor-pointer flex-col gap-1 rounded-md p-2 text-xs',
    style: {
      border: '1px solid var(--ui-stroke-secondary)',
      borderLeft: '3px solid ' + URGENCY_COLOR[card.urgency]
    },
    draggable: true,
    onDragStart: e => e.dataTransfer.setData('text/plain', card.chat_jid),
    onClick: () => onSelect(card.chat_jid),
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-1',
        children: [
          jsx('span', { className: 'truncate font-medium', children: card.contact_name || card.phone }),
          card.priority >= 2 ? jsx(Badge, { variant: 'destructive', children: 'Escalated' }) : null,
          !card.agent_active ? jsx(Badge, { variant: 'warn', children: 'Human' }) : null,
          !STATE_LABELS[card.state] ? jsx(Badge, { variant: 'outline', children: card.state }) : null
        ]
      }),
      jsx('div', { className: 'line-clamp-2', style: secondary, children: card.last_message_preview }),
      jsxs('div', {
        className: 'flex items-center gap-2',
        style: secondary,
        children: [
          jsx('span', { children: fmtAge(card.age_seconds) }),
          card.unread_count > 0 ? jsx(Badge, { variant: 'default', children: String(card.unread_count) }) : null,
          card.state === 'muted' && card.muted_until
            ? jsx('span', { children: 'until ' + fmtTime(card.muted_until) })
            : null
        ]
      })
    ]
  })
}

function ColumnView({ col, count, showCards, onSelect }) {
  const onDrop = e => {
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
  return jsxs('div', {
    className: 'flex shrink-0 flex-col gap-2',
    style: { width: '16rem', minHeight: '8rem' },
    onDragOver: e => e.preventDefault(),
    onDrop,
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2 text-xs font-medium',
        children: [
          jsx('span', { children: stateLabel(col.name) }),
          jsx(Badge, { variant: 'muted', children: String(count) })
        ]
      }),
      ...(showCards ? col.cards.map(card => jsx(CardView, { card, onSelect }, card.chat_jid)) : [])
    ]
  })
}

function DetailPanel({ jid, channel, onClose }) {
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const chat = useQuery({
    queryKey: [ID, 'chat', jid],
    queryFn: () => ctxRef.rest(chatPath(jid)),
    refetchInterval: POLL_MS
  })

  useEffect(() => {
    act(chatPath(jid) + '/read', {})
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

  const shell = children =>
    jsx('div', {
      className: 'flex shrink-0 flex-col gap-2 p-3',
      style: {
        width: '24rem',
        borderLeft: '1px solid var(--ui-stroke-secondary)',
        overflowY: 'auto',
        maxHeight: '100%'
      },
      children
    })

  if (!chat.data) {
    return shell(
      chat.error
        ? jsx(ErrorState, { title: 'Error', description: errorText(chat.error) })
        : jsx(Skeleton, { className: 'h-24 w-full' })
    )
  }

  const { conversation: conv, messages, state_history: history, allowed_states: allowed } = chat.data
  const button = (label, path, body) =>
    jsx(
      Button,
      { size: 'xs', variant: 'secondary', onClick: () => act(chatPath(jid) + path, body), children: label },
      label
    )
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

  return shell([
    jsxs(
      'div',
      {
        className: 'flex items-center justify-between',
        children: [
          jsx('span', { className: 'font-medium', children: conv.contact_name || conv.phone }),
          jsx(Button, { size: 'xs', variant: 'ghost', onClick: onClose, children: '✕' })
        ]
      },
      'head'
    ),
    jsx('div', { className: 'flex flex-wrap gap-1', children: actions }, 'actions'),
    jsx(
      'div',
      {
        className: 'flex flex-col gap-1',
        children: messages.map(m =>
          jsxs(
            'div',
            {
              className: 'rounded-md p-2 text-xs',
              style: {
                maxWidth: '85%',
                marginLeft: m.direction === 'out' ? 'auto' : undefined,
                border: '1px solid var(--ui-stroke-secondary)'
              },
              children: [m.body, jsx('div', { style: secondary, children: fmtTime(m.ts) })]
            },
            m.id
          )
        )
      },
      'thread'
    ),
    jsxs(
      'form',
      {
        className: 'flex flex-col gap-1',
        onSubmit: send,
        children: [
          jsx(Textarea, {
            rows: 3,
            placeholder: 'Write a reply…',
            value: text,
            onChange: e => setText(e.target.value)
          }),
          jsx(Button, {
            size: 'xs',
            variant: 'secondary',
            type: 'submit',
            disabled: !canReply || sending || !text.trim(),
            children: 'Send'
          }),
          !canReply
            ? jsx('div', {
                className: 'text-xs',
                style: secondary,
                children: channel !== 'connected' ? 'WhatsApp channel not connected' : 'Demo chat: replies disabled'
              })
            : null
        ]
      },
      'reply'
    ),
    jsx('span', { className: 'text-xs font-medium', children: 'History' }, 'history-title'),
    ...history.map(r =>
      jsxs(
        'div',
        {
          className: 'text-xs',
          style: secondary,
          children: [
            (r.from_state ? stateLabel(r.from_state) : '—') + ' → ' + stateLabel(r.to_state),
            jsx('div', { children: [r.actor, r.reason, fmtTime(r.at)].filter(Boolean).join(' · ') })
          ]
        },
        'h' + r.id
      )
    )
  ])
}

function BoardPage() {
  const [draft, setDraft] = useState('')
  const [query, setQuery] = useState('')
  const [includeClosed, setIncludeClosed] = useState(false)
  const [selected, setSelected] = useState(null)

  const board = useQuery({
    queryKey: [ID, 'board', query, includeClosed],
    queryFn: () => ctxRef.rest('/board?' + new URLSearchParams({ q: query, include_closed: String(includeClosed) })),
    refetchInterval: POLL_MS
  })
  const stats = useQuery({
    queryKey: [ID, 'stats'],
    queryFn: () => ctxRef.rest('/stats'),
    refetchInterval: POLL_MS
  })

  if (!board.data || !stats.data) {
    const error = board.error || stats.error
    return error
      ? jsx(ErrorState, { title: 'Backend unreachable', description: errorText(error) })
      : jsx(Skeleton, { className: 'h-full w-full' })
  }

  const error = board.error || stats.error
  const [channelText, channelVariant] = CHANNEL_LABELS[stats.data.channel] || CHANNEL_LABELS.unreachable
  return jsxs('div', {
    className: 'flex h-full min-w-0 flex-col gap-2 p-3',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsxs('form', {
            className: 'flex items-center gap-2',
            onSubmit: e => {
              e.preventDefault()
              setQuery(draft)
            },
            children: [
              jsx(Input, {
                value: draft,
                placeholder: 'Search name or phone',
                onChange: e => setDraft(e.target.value)
              }),
              jsx(Button, { size: 'xs', variant: 'secondary', type: 'submit', children: 'Search' })
            ]
          }),
          jsx(Button, {
            size: 'xs',
            variant: 'outline',
            onClick: () => setIncludeClosed(v => !v),
            children: includeClosed ? 'Hide closed' : 'Show closed (' + stats.data.counts.closed + ')'
          }),
          jsx(Badge, { variant: channelVariant, children: channelText })
        ]
      }),
      error
        ? jsx('div', {
            className: 'text-xs',
            style: { color: 'var(--ui-orange)' },
            children: 'Data may be stale: ' + errorText(error)
          })
        : null,
      jsxs('div', {
        className: 'flex min-h-0 flex-1 gap-3',
        children: [
          jsx('div', {
            className: 'flex min-w-0 flex-1 gap-3',
            style: { overflowX: 'auto' },
            children: board.data.columns.map(col => {
              const hidden = col.name === 'closed' && !includeClosed
              return jsx(
                ColumnView,
                {
                  col,
                  count: hidden ? stats.data.counts.closed : col.cards.length,
                  showCards: !hidden,
                  onSelect: setSelected
                },
                col.name
              )
            })
          }),
          selected
            ? jsx(
                DetailPanel,
                { jid: selected, channel: stats.data.channel, onClose: () => setSelected(null) },
                selected
              )
            : null
        ]
      })
    ]
  })
}

// Statusbar chip: unanswered "new" count + age of the oldest unanswered chat.
function StatsChip() {
  const { data } = useQuery({
    queryKey: [ID, 'stats'],
    queryFn: () => ctxRef.rest('/stats'),
    refetchInterval: POLL_MS
  })
  if (!data) {
    return null
  }
  const oldest = fmtAge(data.oldest_unanswered_age_seconds)
  return jsxs('button', {
    type: 'button',
    className: 'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem]',
    style: secondary,
    onClick: () => {
      haptic('tap')
      host.navigate(ROUTE)
    },
    children: [jsx(StatusDot, {}), jsx('span', { children: 'WA ' + data.counts.new + (oldest ? ' · ' + oldest : '') })]
  })
}

// --- Register ----------------------------------------------------------------

export default {
  id: ID,
  name: 'Hermes WhatsApp Chat',
  defaultEnabled: false,
  register(ctx) {
    ctxRef = ctx

    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        title: 'Conversations',
        data: { path: ROUTE },
        render: () => jsx(BoardPage, {})
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        data: { path: ROUTE, label: 'Conversations', codicon: 'comment-discussion' }
      },
      {
        id: 'palette',
        area: PALETTE_AREA,
        data: {
          id: 'wa-board.open',
          label: 'Open Conversations',
          keywords: ['wa', 'whatsapp', 'board', 'conversations'],
          run: () => host.navigate(ROUTE)
        }
      },
      { id: 'chip', area: STATUSBAR_AREAS.right, order: 140, render: () => jsx(StatsChip, {}) }
    ])

    // Live updates: an accelerator over the 5s polling (a no-op on OAuth remotes).
    ctx.socket('/events', frame => {
      refresh()
      for (const e of (frame && frame.events) || []) {
        if (e.to_state === 'new' && e.from_state !== 'new') {
          ctx.os.notify({ title: 'New conversation', body: e.contact_name || e.chat_jid, activate: ROUTE })
        }
      }
    })
  }
}
