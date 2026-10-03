/**
 * hermes-whatsapp-chat — desktop plugin v2 (ESM, loaded uncompiled).
 *
 * Full WhatsApp chat app on /wa-board: Chats | Board | Settings.
 * Rules: jsx()/jsxs() calls only (no JSX syntax). Only these imports resolve:
 * @hermes/plugin-sdk, react, react/jsx-runtime. Colors via --ui-* vars only.
 * Hot-reloads on save; if not: ⌘K → "Reload desktop plugins".
 *
 * `SettingsPage` is defined in a separate file that is pasted into this module.
 */

import {
  atom,
  Badge,
  Button,
  Codicon,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  ErrorState,
  haptic,
  host,
  Input,
  isSubmitEnter,
  PALETTE_AREA,
  profileColor,
  profileColorSoft,
  queryClient,
  ROUTES_AREA,
  SegmentedControl,
  SIDEBAR_NAV_AREA,
  Skeleton,
  STATUSBAR_AREAS,
  StatusDot,
  Textarea,
  useQuery,
  useValue,
  WorkspacePageHeaderControl
} from '@hermes/plugin-sdk'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'hermes-whatsapp-chat'
const ROUTE = '/wa-board'
const POLL_MS = 5000
const PAGE_SIZE = 50
const HEARTBEAT_STALE_S = 15
const WA_JID_RE = /@(s\.whatsapp\.net|lid)$/

// Set by register(); handlers read it imperatively, never from render closures.
let ctxRef = null

// Per-route UI state (also driven by notification clicks): the main page, every per-number
// page and every split tile keep their own tab, selected conversation and number filter.
// `account` is the WhatsApp number id, or null for "All numbers".
const scopes = new Map()

function getScope(route, accountId) {
  let scope = scopes.get(route)
  if (!scope) {
    scope = { tab: atom('chats'), selected: atom(null), account: atom(accountId) }
    scopes.set(route, scope)
  }
  return scope
}

// Service (re)start progress after Install service / Reinstall: null | 'restarting' | 'failed'.
const $restart = atom(null)

// --- Shared helpers for SettingsPage -----------------------------------------
// Module scope, consumed by the SettingsPage component pasted into this file:
//   ID                      plugin id (also the first React Query key segment)
//   rest(path, opts)        ctxRef.rest passthrough (GET by default)
//   act(path, body, method) POST/PUT/DELETE + error toast + awaits refresh();
//                           resolves the response data (or true) / false on failure
//   errorText(err)          readable message from a ctx.rest rejection (detail only)
//   failureText(err)        toast text: "HTTP <status>: <detail>" (+ restart hint for 404/405)
//   fmtAge(seconds)         "now" / "5m" / "3h" / "2d"
//   fmtTime(unixSeconds)    viewer-locale short date + time
//   refresh()               invalidates every [ID, …] query (returns a promise)
//   useApi(key, path, opts) useQuery wrapper, refetches every POLL_MS
//   STATE_LABELS            conversation state id → label
//   ACCOUNT_COLORS          account color name → var(--ui-*) css color
//   AccountDot({account})   colored dot for an account (Account or conversation Card)
//   ServiceBanner()         "WhatsApp service not installed / not running / restarting" + Install / Reinstall button
//   OutdatedBanner()        "Restart Hermes…" when /health api_version < REQUIRED_API_VERSION
//   useBackendOutdated()    true while the running backend is older than this UI
//   useServiceAction()      [busyKey, run(key, path, doneMessage)] for service/skill POST routes;
//                           'install' then waits for a fresh heartbeat (progress in $restart)
//   SERVICE_LOG_HINT        where the service writes its log (shown on errors)

const STATE_LABELS = { new: 'New', in_progress: 'In progress', waiting: 'Waiting', muted: 'Muted', closed: 'Closed' }

const ACCOUNT_COLORS = {
  red: 'var(--ui-red)',
  orange: 'var(--ui-orange)',
  yellow: 'var(--ui-yellow)',
  green: 'var(--ui-green)',
  teal: 'var(--ui-cyan)',
  cyan: 'var(--ui-cyan)',
  blue: 'var(--ui-blue)',
  purple: 'var(--ui-purple)',
  pink: 'color-mix(in srgb, var(--ui-red) 55%, var(--ui-purple))',
  gray: 'var(--ui-text-quaternary)'
}

function rest(path, opts) {
  return ctxRef.rest(path, opts)
}

const refresh = () => queryClient.invalidateQueries({ queryKey: [ID] })

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

function errorStatus(err) {
  if (err && typeof err.status === 'number') {
    return err.status
  }
  const m = /^(\d{3}):/.exec(String((err && err.message) || err))
  return m ? Number(m[1]) : null
}

// 404/405 on a plugin route usually means Hermes still runs the previous backend.
function restartHint(err) {
  const status = errorStatus(err)
  return status === 404 || status === 405 ? ' — restart Hermes to load the updated plugin' : ''
}

function failureText(err) {
  const status = errorStatus(err)
  return (status ? 'HTTP ' + status + ': ' : '') + (errorText(err) || 'Request failed') + restartHint(err)
}

async function call(path, body, method, errTitle) {
  let out
  try {
    const opts = { method, timeoutMs: 90000 }
    if (body !== undefined) {
      opts.body = body
    }
    const data = await ctxRef.rest(path, opts)
    out = data === undefined || data === null ? true : data
  } catch (err) {
    host.notify({ kind: 'error', title: errTitle, message: failureText(err) })
    out = false
  }
  await refresh()
  return out
}

const act = (path, body, method = 'POST') => call(path, body, method, 'Action failed')

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

// key: string | array of key segments; opts: ctx.rest options plus { enabled }.
function useApi(key, path, opts) {
  const { enabled = true, ...restOpts } = opts || {}
  const hasOpts = Object.keys(restOpts).length > 0
  return useQuery({
    queryKey: [ID, ...[].concat(key), path],
    queryFn: () => ctxRef.rest(path, hasOpts ? restOpts : undefined),
    enabled,
    refetchInterval: POLL_MS
  })
}

function AccountDot({ account, size = 8 }) {
  const name = account ? account.color || account.account_color : null
  return h('span', {
    title: account ? account.label || account.account_label || '' : '',
    style: {
      display: 'inline-block',
      flex: '0 0 auto',
      width: size,
      height: size,
      borderRadius: '50%',
      background: ACCOUNT_COLORS[name] || ACCOUNT_COLORS.gray
    }
  })
}

const SERVICE_LOG_HINT = '~/.hermes/plugin-data/hermes-whatsapp-chat/logs/channel.log'

const SERVICE_START_TIMEOUT_MS = 30000
const REQUIRED_API_VERSION = 4
const RESTART_TITLE = 'Restart Hermes to finish installing or updating WhatsApp Chat'
const RESTART_BODY =
  'The plugin was updated but Hermes is still running the previous backend. Quit Hermes (⌘Q) and open it again.'

// True when the running backend is older than this UI: /health lacks api_version, reports a lower one,
// or the route does not exist at all (404, plugin routes are mounted only at Hermes startup).
function useBackendOutdated() {
  const q = useApi('health', '/health')
  if (!q.data) {
    return errorStatus(q.error) === 404
  }
  const v = q.data.api_version
  return typeof v !== 'number' || v < REQUIRED_API_VERSION
}

function OutdatedBanner() {
  const outdated = useBackendOutdated()
  if (!outdated) {
    return null
  }
  return h(
    'div',
    {
      style: {
        ...BANNER,
        ...F.col,
        gap: 4,
        border: '1px solid var(--ui-red)',
        background: 'color-mix(in srgb, var(--ui-red) 10%, transparent)'
      }
    },
    h('div', { style: { fontWeight: 600 } }, RESTART_TITLE),
    h('div', { style: T.muted }, RESTART_BODY)
  )
}

function sleep(ms) {
  return new Promise(resolve => ctxRef.setTimeout(resolve, ms))
}

// Resolves true once /service reports a heartbeat newer than `since` (unix seconds), false after the timeout.
async function waitForHeartbeat(since) {
  const deadline = Date.now() + SERVICE_START_TIMEOUT_MS
  while (Date.now() < deadline) {
    await sleep(1000)
    try {
      const s = await rest('/service')
      if (s && s.heartbeat_at > since) {
        return true
      }
    } catch {
      // keep polling until the deadline
    }
  }
  return false
}

// POSTs a /service/* or /skill/* route; act() toasts failures and refreshes every query.
// After 'install' it keeps `busy` set until the restarted service reports a fresh heartbeat.
function useServiceAction() {
  const [busy, setBusy] = useState(null)
  const run = async (key, path, doneMessage) => {
    haptic('tap')
    setBusy(key)
    try {
      const since = Math.floor(Date.now() / 1000)
      const ok = await act(path, undefined)
      if (ok && key === 'install') {
        $restart.set('restarting')
        const started = await waitForHeartbeat(since)
        $restart.set(started ? null : 'failed')
        await refresh()
        if (!started) {
          host.notify({
            kind: 'error',
            title: 'Service did not start',
            message: 'No new heartbeat after 30 s. Check the log: ' + SERVICE_LOG_HINT
          })
          return
        }
      }
      if (ok && doneMessage) {
        host.notify({ kind: 'info', message: doneMessage })
      }
    } finally {
      setBusy(null)
    }
  }
  return [busy, run]
}

function ServiceBanner() {
  const q = useApi('service', '/service')
  const [busy, run] = useServiceAction()
  const outdated = useBackendOutdated()
  const restart = useValue($restart)
  if (!q.data || q.data.running || outdated) {
    return null
  }
  const installed = q.data.installed !== false
  const restarting = restart === 'restarting'
  return h(
    'div',
    { style: { ...BANNER, ...F.col, gap: 4 } },
    h(
      'div',
      { style: { fontWeight: 600 } },
      restarting
        ? 'Restarting service…'
        : installed
          ? 'WhatsApp service is not running'
          : 'WhatsApp service is not installed'
    ),
    h(
      'div',
      { style: T.muted },
      restarting
        ? 'Waiting for the service to report in. This can take up to 30 seconds.'
        : installed
          ? 'Messages are neither received nor sent until the background service runs. Reinstalling restarts it.'
          : 'Messages are neither received nor sent until the background service is installed. It starts automatically and keeps running in the background.'
    ),
    restart === 'failed'
      ? h(
          'div',
          { style: T.warn },
          'The service did not start within 30 s. Check the log: ',
          h('code', { style: CODE }, SERVICE_LOG_HINT)
        )
      : installed && !restarting
        ? h(
            'div',
            { style: T.muted },
            'If it keeps failing, check the log: ',
            h('code', { style: CODE }, SERVICE_LOG_HINT)
          )
        : null,
    h(
      'div',
      { style: { ...F.row, gap: 6 } },
      h(
        Button,
        {
          size: 'xs',
          variant: 'secondary',
          loading: busy === 'install' || restarting,
          disabled: busy !== null || restarting,
          onClick: () => run('install', '/service/install', 'WhatsApp service installed')
        },
        installed ? 'Reinstall' : 'Install service'
      )
    )
  )
}

// --- Local helpers -----------------------------------------------------------

const STATE_VARIANT = { new: 'default', in_progress: 'success', waiting: 'warn', muted: 'muted', closed: 'outline' }

const ACCOUNT_STATE_LABELS = {
  demo: 'Demo',
  service_down: 'Service down',
  stopped: 'Stopped',
  starting: 'Starting',
  pairing: 'Pairing',
  qr: 'Scan QR',
  connecting: 'Connecting',
  connected: 'Connected',
  disconnected: 'Disconnected',
  logged_out: 'Logged out',
  error: 'Error'
}

const URGENCY_COLOR = { high: 'var(--ui-red)', medium: 'var(--ui-orange)', normal: 'var(--ui-stroke-secondary)' }

const BORDER = '1px solid var(--ui-stroke-secondary)'

const F = {
  row: { display: 'flex', alignItems: 'center', gap: 8 },
  col: { display: 'flex', flexDirection: 'column' },
  fill: { flex: '1 1 auto', minWidth: 0, minHeight: 0 },
  ellipsis: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }
}

const T = {
  muted: { color: 'var(--ui-text-tertiary)', fontSize: 11 },
  secondary: { color: 'var(--ui-text-secondary)', fontSize: 12 },
  warn: { color: 'var(--ui-orange)', fontSize: 11 }
}

const BANNER = {
  margin: '8px 12px 0',
  padding: '8px 10px',
  fontSize: 12,
  borderRadius: 6,
  border: '1px solid var(--ui-orange)',
  background: 'color-mix(in srgb, var(--ui-orange) 10%, transparent)'
}

const CODE = {
  padding: '2px 6px',
  borderRadius: 4,
  fontSize: 11,
  whiteSpace: 'nowrap',
  background: 'var(--ui-inline-code-background)',
  color: 'var(--ui-inline-code-foreground)'
}

const CHIP = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 5,
  padding: '2px 8px',
  borderRadius: 999,
  fontSize: 11,
  lineHeight: '16px',
  cursor: 'pointer',
  border: BORDER,
  background: 'transparent',
  color: 'var(--ui-text-secondary)',
  whiteSpace: 'nowrap'
}

const CHIP_ACTIVE = {
  background: 'color-mix(in srgb, var(--ui-accent) 16%, transparent)',
  color: 'var(--ui-text-primary)',
  borderColor: 'var(--ui-accent)'
}

// jsx()/jsxs() with createElement-like ergonomics; `key` is lifted out of props.
function h(type, props, ...children) {
  const { key, ...rest } = props || {}
  if (children.length === 0) {
    return jsx(type, rest, key)
  }
  if (children.length === 1) {
    return jsx(type, { ...rest, children: children[0] }, key)
  }
  return jsxs(type, { ...rest, children }, key)
}

const nowSec = () => Math.floor(Date.now() / 1000)
const stateLabel = s => STATE_LABELS[s] || s

function fmtPhone(p) {
  if (!p) {
    return ''
  }
  return /^\d+$/.test(p) ? '+' + p : p
}

function displayName(c) {
  return (
    c.contact_name ||
    fmtPhone(c.phone) ||
    (/@lid$/.test(c.chat_jid || '') ? 'Unknown contact' : c.chat_jid) ||
    'Unknown'
  )
}

function initials(name) {
  const clean = String(name || '')
    .replace(/[^\p{L}\p{N} ]/gu, '')
    .trim()
  if (!clean || /^[\d ]+$/.test(clean)) {
    return '#'
  }
  const words = clean.split(/\s+/)
  if (words.length === 1) {
    return words[0].slice(0, 2).toUpperCase()
  }
  return (words[0][0] + words[1][0]).toUpperCase()
}

function fmtClock(ts) {
  return new Date(ts * 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

// status -> [codicon, color, label]; delivery ticks like WhatsApp (read/played are blue).
const TICK_ICONS = {
  pending: ['clock', null, 'Sending'],
  sent: ['check', null, 'Sent'],
  delivered: ['check-all', null, 'Delivered'],
  read: ['check-all', 'var(--ui-blue)', 'Read'],
  played: ['check-all', 'var(--ui-blue)', 'Played']
}

function tickTitle(m) {
  if (m.status === 'pending') {
    return 'Sending'
  }
  let title = 'Sent ' + fmtClock(m.ts)
  if (m.delivered_at) {
    title += ' · Delivered ' + fmtClock(m.delivered_at)
  }
  if (m.read_at) {
    title += (m.status === 'played' ? ' · Played ' : ' · Read ') + fmtClock(m.read_at)
  }
  return title
}

function StatusTicks({ status, title, style }) {
  const tick = TICK_ICONS[status]
  if (!tick) {
    return null
  }
  return h(Codicon, { name: tick[0], title: title || tick[2], style: tick[1] ? { ...style, color: tick[1] } : style })
}

const dayStart = d => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()
const dayDiff = ts => Math.round((dayStart(new Date()) - dayStart(new Date(ts * 1000))) / 86400000)

function dayKey(ts) {
  const d = new Date(ts * 1000)
  return d.getFullYear() + '-' + d.getMonth() + '-' + d.getDate()
}

function fmtDay(ts) {
  const diff = dayDiff(ts)
  if (diff === 0) {
    return 'Today'
  }
  if (diff === 1) {
    return 'Yesterday'
  }
  const d = new Date(ts * 1000)
  return d.toLocaleDateString(undefined, {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
    year: d.getFullYear() === new Date().getFullYear() ? undefined : 'numeric'
  })
}

function fmtListTime(ts) {
  if (!ts) {
    return ''
  }
  const diff = dayDiff(ts)
  if (diff === 0) {
    return fmtClock(ts)
  }
  if (diff === 1) {
    return 'Yesterday'
  }
  const d = new Date(ts * 1000)
  if (diff < 7) {
    return d.toLocaleDateString(undefined, { weekday: 'short' })
  }
  return d.toLocaleDateString(undefined, { dateStyle: 'short' })
}

function fmtSize(n) {
  if (!n && n !== 0) {
    return ''
  }
  if (n < 1024) {
    return n + ' B'
  }
  if (n < 1048576) {
    return Math.round(n / 1024) + ' KB'
  }
  return (n / 1048576).toFixed(1) + ' MB'
}

function fmtHours(hours) {
  if (hours % 24 === 0) {
    return hours / 24 + 'd'
  }
  return hours + 'h'
}

function authorLabel(author) {
  if (!author || author === 'user') {
    return 'You'
  }
  if (author === 'phone') {
    return 'Phone'
  }
  if (author === 'auto_reply') {
    return 'Auto-reply'
  }
  if (author === 'cli') {
    return 'CLI'
  }
  if (author.startsWith('agent:')) {
    const profile = author.slice(6)
    return !profile || profile === 'default' ? 'Agent' : 'Agent ' + profile
  }
  if (author.startsWith('rule:')) {
    return 'Rule #' + author.slice(5)
  }
  return author
}

function previewPrefix(c) {
  if (c.last_message_direction !== 'out') {
    return ''
  }
  const a = c.last_message_author || 'user'
  if (a.startsWith('agent:') || a === 'cli') {
    return 'Agent: '
  }
  if (a.startsWith('rule:') || a === 'auto_reply') {
    return 'Auto: '
  }
  return 'You: '
}

function mediaKind(item) {
  const mime = item.mime || ''
  const type = item.type || ''
  if (mime.startsWith('image/') || type === 'image' || type === 'sticker') {
    return 'image'
  }
  if (mime.startsWith('audio/') || type === 'audio' || type === 'ptt') {
    return 'audio'
  }
  if (mime.startsWith('video/') || type === 'video') {
    return 'video'
  }
  return 'document'
}

function parsePayload(raw) {
  if (raw && typeof raw === 'object') {
    return raw
  }
  try {
    const v = JSON.parse(raw)
    return v && typeof v === 'object' ? v : {}
  } catch {
    return {}
  }
}

// quiet = {start: "22:00", end: "08:00"} in the viewer's local time.
function inQuietHours(quiet, date) {
  if (!quiet || !quiet.start || !quiet.end) {
    return false
  }
  const toMin = s => {
    const [hh, mm] = String(s).split(':')
    return Number(hh) * 60 + Number(mm || 0)
  }
  const start = toMin(quiet.start)
  const end = toMin(quiet.end)
  const now = date.getHours() * 60 + date.getMinutes()
  if (Number.isNaN(start) || Number.isNaN(end) || start === end) {
    return false
  }
  return start < end ? now >= start && now < end : now >= start || now < end
}

// Effective connection state of an account, honoring the heartbeat rule.
function accountState(a) {
  if (a.kind === 'demo') {
    return 'demo'
  }
  const st = a.status
  if (!st) {
    return 'stopped'
  }
  if (st.state === 'stopped' || st.state === 'logged_out') {
    return st.state
  }
  if (!st.heartbeat_at || nowSec() - st.heartbeat_at > HEARTBEAT_STALE_S) {
    return 'service_down'
  }
  return st.state
}

function accountStateColor(state) {
  if (state === 'connected' || state === 'demo') {
    return 'var(--ui-green)'
  }
  if (state === 'stopped' || state === 'logged_out') {
    return 'var(--ui-text-tertiary)'
  }
  if (state === 'error' || state === 'disconnected' || state === 'service_down') {
    return 'var(--ui-red)'
  }
  return 'var(--ui-orange)'
}

// Reason sending is impossible, or null when the conversation can send.
function sendBlock(data, service) {
  const c = data.conversation
  const account = data.account
  if (account.kind === 'demo' || !WA_JID_RE.test(c.chat_jid)) {
    return 'Demo conversation: replies are disabled'
  }
  if (service && service.running === false) {
    return 'WhatsApp service is not running'
  }
  if (account.desired !== 'running') {
    return 'Account "' + account.label + '" is stopped'
  }
  const st = accountState(account)
  if (st === 'service_down') {
    return 'WhatsApp service is not running'
  }
  if (st !== 'connected') {
    return 'Account "' + account.label + '" is not connected (' + (ACCOUNT_STATE_LABELS[st] || st) + ')'
  }
  return null
}

function readFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(reader.error || new Error('Could not read file'))
    reader.onload = () => {
      const url = String(reader.result)
      resolve({
        name: file.name,
        mime: file.type || 'application/octet-stream',
        size: file.size,
        data_base64: url.slice(url.indexOf(',') + 1)
      })
    }
    reader.readAsDataURL(file)
  })
}

// Unsent composer text per conversation, survives switching chats.
const composerText = new Map()

// Loading / error / stale feedback shared by every data view.
function gate(q, render) {
  if (!q.data) {
    return q.error
      ? h(ErrorState, { title: 'Backend unreachable', description: errorText(q.error) })
      : h(Skeleton, { className: 'h-24 w-full' })
  }
  return h(
    'div',
    { style: { ...F.col, ...F.fill } },
    q.error
      ? h('div', { style: { ...T.warn, padding: '4px 12px' } }, 'Data may be stale: ' + errorText(q.error))
      : null,
    render(q.data)
  )
}

// --- Small UI pieces -----------------------------------------------------------

function Chip({ active, onClick, title, children }) {
  return h('button', { type: 'button', title, onClick, style: active ? { ...CHIP, ...CHIP_ACTIVE } : CHIP }, children)
}

function Avatar({ name, size = 36 }) {
  const color = profileColor(name) || 'var(--ui-text-tertiary)'
  return h(
    'div',
    {
      style: {
        width: size,
        height: size,
        flex: '0 0 auto',
        borderRadius: '50%',
        ...F.row,
        justifyContent: 'center',
        gap: 0,
        fontSize: Math.round(size * 0.36),
        fontWeight: 600,
        color,
        background: profileColorSoft(color, 18)
      }
    },
    initials(name)
  )
}

function Section({ title, children }) {
  return h(
    'div',
    { style: { ...F.col, gap: 6, padding: '10px 12px', borderBottom: BORDER } },
    h('div', { style: { fontSize: 11, fontWeight: 600, textTransform: 'uppercase', ...T.muted } }, title),
    children
  )
}

function KV({ label, children }) {
  return h(
    'div',
    { style: { display: 'flex', gap: 8, fontSize: 12 } },
    h('span', { style: { ...T.muted, width: 84, flex: '0 0 auto' } }, label),
    h('span', { style: { minWidth: 0, wordBreak: 'break-word' } }, children)
  )
}

function Menu({ label, items, disabled, title }) {
  const [open, setOpen] = useState(false)
  const box = useRef(null)
  useEffect(() => {
    if (!open) {
      return undefined
    }
    return ctxRef.addEventListener(document, 'mousedown', e => {
      if (box.current && !box.current.contains(e.target)) {
        setOpen(false)
      }
    })
  }, [open])
  return h(
    'div',
    { ref: box, style: { position: 'relative' } },
    h(
      Button,
      {
        size: 'xs',
        variant: 'secondary',
        disabled: disabled || items.length === 0,
        title,
        onClick: () => setOpen(v => !v)
      },
      label + ' ▾'
    ),
    open
      ? h(
          'div',
          {
            style: {
              position: 'absolute',
              top: '100%',
              right: 0,
              zIndex: 30,
              minWidth: 150,
              marginTop: 4,
              padding: 4,
              ...F.col,
              gap: 2,
              borderRadius: 6,
              border: BORDER,
              background: 'var(--ui-bg-elevated)',
              boxShadow: '0 6px 20px color-mix(in srgb, var(--ui-base) 18%, transparent)'
            }
          },
          items.map(item =>
            h(
              Button,
              {
                key: item.key,
                size: 'xs',
                variant: 'ghost',
                style: { justifyContent: 'flex-start', width: '100%' },
                onClick: () => {
                  setOpen(false)
                  item.onClick()
                }
              },
              item.label
            )
          )
        )
      : null
  )
}

function CardBadges({ c }) {
  return [
    c.priority >= 2 ? h(Badge, { key: 'esc', variant: 'destructive', size: 'xs' }, 'Escalated') : null,
    !c.agent_active ? h(Badge, { key: 'human', variant: 'warn', size: 'xs' }, 'Human') : null,
    h(Badge, { key: 'state', variant: STATE_VARIANT[c.state] || 'outline', size: 'xs' }, stateLabel(c.state))
  ]
}

// --- Conversation list ---------------------------------------------------------

function ConvRow({ c, selected, onSelect, snippet }) {
  const name = displayName(c)
  const urgent = c.urgency && c.urgency !== 'normal'
  return h(
    'div',
    {
      role: 'button',
      tabIndex: 0,
      onClick: () => onSelect(c.id),
      onKeyDown: e => {
        if (isSubmitEnter(e)) {
          onSelect(c.id)
        }
      },
      style: {
        display: 'flex',
        gap: 10,
        padding: '8px 10px',
        cursor: 'pointer',
        borderBottom: BORDER,
        borderLeft: '3px solid ' + (urgent ? URGENCY_COLOR[c.urgency] : 'transparent'),
        background: selected ? 'var(--ui-row-active-background)' : 'transparent'
      }
    },
    h(Avatar, { name: c.contact_name || '' }),
    h(
      'div',
      { style: { ...F.col, gap: 2, ...F.fill } },
      h(
        'div',
        { style: { ...F.row, gap: 6 } },
        h(
          'span',
          { style: { ...F.ellipsis, flex: '1 1 auto', fontSize: 13, fontWeight: c.unread_count > 0 ? 700 : 500 } },
          name
        ),
        h('span', { style: { ...T.muted, flex: '0 0 auto' } }, fmtListTime(c.last_message_at))
      ),
      h(
        'div',
        { style: { ...F.row, gap: 6 } },
        h(AccountDot, { account: c }),
        h(
          'span',
          { style: { ...F.ellipsis, flex: '1 1 auto', fontSize: 12, color: 'var(--ui-text-secondary)' } },
          c.has_draft ? h('span', { style: { color: 'var(--ui-orange)', fontWeight: 600 } }, 'Draft · ') : null,
          c.last_message_status && snippet === undefined
            ? h(StatusTicks, { status: c.last_message_status, style: { marginRight: 4 } })
            : null,
          snippet !== undefined ? snippet : previewPrefix(c) + (c.last_message_preview || '')
        ),
        c.unread_count > 0 ? h(Badge, { variant: 'solid', size: 'xs' }, String(c.unread_count)) : null
      ),
      h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 4 } }, h(CardBadges, { c }))
    )
  )
}

function ConvChunk({ offset, params, selectedId, onSelect, onTotal }) {
  const qs = new URLSearchParams({ ...params, limit: String(PAGE_SIZE), offset: String(offset) })
  const q = useApi('conversations', '/conversations?' + qs)
  const total = q.data ? q.data.total : null
  useEffect(() => {
    if (offset === 0 && total !== null) {
      onTotal(total)
    }
  }, [offset, total, onTotal])
  if (!q.data) {
    return q.error
      ? h(ErrorState, { title: 'Backend unreachable', description: errorText(q.error) })
      : h(
          'div',
          { style: { ...F.col, gap: 6, padding: 10 } },
          h(Skeleton, { className: 'h-12 w-full' }),
          h(Skeleton, { className: 'h-12 w-full' }),
          h(Skeleton, { className: 'h-12 w-full' })
        )
  }
  const rows = q.data.conversations
  return h(
    'div',
    null,
    q.error
      ? h('div', { style: { ...T.warn, padding: '4px 10px' } }, 'Data may be stale: ' + errorText(q.error))
      : null,
    offset === 0 && rows.length === 0
      ? h('div', { style: { ...T.muted, padding: 16, textAlign: 'center' } }, 'No conversations')
      : null,
    rows.map(c => h(ConvRow, { key: c.id, c, selected: c.id === selectedId, onSelect }))
  )
}

function ConvList({ params, selectedId, onSelect }) {
  const [pages, setPages] = useState(1)
  const [total, setTotal] = useState(0)
  const onTotal = useCallback(n => setTotal(n), [])
  const chunks = []
  for (let i = 0; i < pages; i++) {
    chunks.push(h(ConvChunk, { key: i, offset: i * PAGE_SIZE, params, selectedId, onSelect, onTotal }))
  }
  return h(
    'div',
    null,
    chunks,
    pages * PAGE_SIZE < total
      ? h(
          'div',
          { style: { padding: 10, textAlign: 'center' } },
          h(
            Button,
            { size: 'xs', variant: 'outline', onClick: () => setPages(p => p + 1) },
            'Load more (' + (total - pages * PAGE_SIZE) + ' left)'
          )
        )
      : null
  )
}

function SearchResults({ q, accountId, selectedId, onSelect }) {
  const qs = new URLSearchParams({ q })
  if (accountId) {
    qs.set('account_id', String(accountId))
  }
  const res = useApi('search', '/search?' + qs, { enabled: q.length >= 2 })
  if (q.length < 2) {
    return h(
      'div',
      { style: { ...T.muted, padding: 16, textAlign: 'center' } },
      'Type at least 2 characters to search messages'
    )
  }
  return gate(res, data =>
    data.results.length === 0
      ? h('div', { style: { ...T.muted, padding: 16, textAlign: 'center' } }, 'No messages found')
      : data.results.map(r => {
          const body = (r.message.body || '').replace(/\s+/g, ' ').trim()
          const prefix = r.message.direction === 'out' ? authorLabel(r.message.author) + ': ' : ''
          return h(ConvRow, {
            key: r.message.id,
            c: { ...r.conversation, last_message_at: r.message.ts, has_draft: false, unread_count: 0 },
            selected: r.conversation.id === selectedId,
            onSelect,
            snippet: prefix + (body.length > 140 ? body.slice(0, 140) + '…' : body)
          })
        })
  )
}

function ConvSidebar({ accountId, stateFilter, setStateFilter, selectedId, onSelect }) {
  const [qInput, setQInput] = useState('')
  const [q, setQ] = useState('')
  const [mode, setMode] = useState('chats')

  useEffect(() => ctxRef.setTimeout(() => setQ(qInput.trim()), 250), [qInput])

  const params = useMemo(() => {
    const p = {}
    if (accountId) {
      p.account_id = String(accountId)
    }
    if (stateFilter === 'unread') {
      p.unread_only = 'true'
    } else if (stateFilter) {
      p.state = stateFilter
    }
    if (mode === 'chats' && q) {
      p.q = q
    }
    return p
  }, [accountId, stateFilter, mode, q])

  const filters = [['', 'All'], ['unread', 'Unread'], ...Object.keys(STATE_LABELS).map(s => [s, STATE_LABELS[s]])]

  return h(
    'div',
    { style: { ...F.col, width: '21rem', flex: '0 0 auto', minHeight: 0, borderRight: BORDER } },
    h(
      'div',
      { style: { ...F.col, gap: 8, padding: 10, borderBottom: BORDER } },
      h(Input, {
        value: qInput,
        placeholder: mode === 'chats' ? 'Search name or phone' : 'Search messages',
        onChange: e => setQInput(e.target.value)
      }),
      h(SegmentedControl, {
        options: [
          { id: 'chats', label: 'Conversations' },
          { id: 'messages', label: 'Search messages' }
        ],
        value: mode,
        onChange: setMode
      }),
      mode === 'chats'
        ? h(
            'div',
            { style: { display: 'flex', flexWrap: 'wrap', gap: 4 } },
            filters.map(([id, label]) =>
              h(Chip, { key: id, active: stateFilter === id, onClick: () => setStateFilter(id) }, label)
            )
          )
        : null
    ),
    h(
      'div',
      { style: { flex: '1 1 auto', minHeight: 0, overflowY: 'auto' } },
      mode === 'messages'
        ? h(SearchResults, { q, accountId, selectedId, onSelect })
        : h(ConvList, { key: JSON.stringify(params), params, selectedId, onSelect })
    )
  )
}

// --- Thread ---------------------------------------------------------------------

function MediaItem({ msg, item, auto, onLoad }) {
  const kind = mediaKind(item)
  const [open, setOpen] = useState(Boolean(auto) && kind === 'image')
  const [busy, setBusy] = useState(false)
  const q = useQuery({
    queryKey: ['hwc-media', msg.id, item.index],
    queryFn: () => ctxRef.rest('/messages/' + msg.id + '/media/' + item.index),
    enabled: open && item.available && kind !== 'document',
    staleTime: Infinity,
    gcTime: 300000,
    retry: false
  })
  const label = item.name || kind
  const size = item.size ? ' (' + fmtSize(item.size) + ')' : ''

  if (!item.available) {
    return h(
      'div',
      { style: { ...T.muted, padding: '6px 8px', borderRadius: 6, border: '1px dashed var(--ui-stroke-secondary)' } },
      'Media unavailable' + (item.name ? ': ' + item.name : '')
    )
  }

  if (kind === 'document') {
    const download = async () => {
      setBusy(true)
      try {
        const data = await ctxRef.rest('/messages/' + msg.id + '/media/' + item.index)
        const a = document.createElement('a')
        a.href = data.data_url
        a.download = data.name || item.name || 'file'
        document.body.appendChild(a)
        a.click()
        a.remove()
      } catch (err) {
        host.notify({ kind: 'error', title: 'Download failed', message: failureText(err) })
      } finally {
        setBusy(false)
      }
    }
    return h(
      Button,
      { size: 'xs', variant: 'secondary', loading: busy, onClick: download, style: { alignSelf: 'flex-start' } },
      h(Codicon, { name: 'file' }),
      label + size
    )
  }

  if (!open) {
    return h(
      Button,
      { size: 'xs', variant: 'secondary', onClick: () => setOpen(true), style: { alignSelf: 'flex-start' } },
      h(Codicon, { name: kind === 'image' ? 'file-media' : kind === 'audio' ? 'unmute' : 'device-camera-video' }),
      (kind === 'image' ? 'Show image' : kind === 'audio' ? 'Load audio' : 'Load video') + size
    )
  }
  if (q.error) {
    return h(
      'div',
      { style: T.warn },
      'Could not load media: ' + errorText(q.error) + ' ',
      h(Button, { size: 'xs', variant: 'ghost', onClick: () => q.refetch() }, 'Retry')
    )
  }
  if (!q.data) {
    return h(Skeleton, { className: 'h-24 w-48' })
  }
  if (kind === 'image') {
    return h('img', {
      src: q.data.data_url,
      alt: label,
      onLoad,
      style: { maxWidth: 280, maxHeight: 320, borderRadius: 6, objectFit: 'contain' }
    })
  }
  if (kind === 'audio') {
    return h('audio', { controls: true, src: q.data.data_url, style: { maxWidth: 260 } })
  }
  return h('video', {
    controls: true,
    src: q.data.data_url,
    onLoadedMetadata: onLoad,
    style: { maxWidth: 280, borderRadius: 6 }
  })
}

function DraftCard({ m, blocked }) {
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(m.body || '')
  const [busy, setBusy] = useState(false)
  const approve = async () => {
    setBusy(true)
    const edited = text.trim() !== (m.body || '').trim()
    await act('/messages/' + m.id + '/approve', edited ? { text: text.trim() } : {})
    setBusy(false)
  }
  const discard = async () => {
    setBusy(true)
    await act('/messages/' + m.id + '/discard', {})
    setBusy(false)
  }
  return h(
    'div',
    { style: { display: 'flex', justifyContent: 'flex-end', padding: '4px 0' } },
    h(
      'div',
      {
        style: {
          ...F.col,
          gap: 6,
          width: '75%',
          maxWidth: 520,
          padding: 10,
          borderRadius: 8,
          border: '1px dashed var(--ui-orange)',
          background: 'color-mix(in srgb, var(--ui-orange) 8%, transparent)'
        }
      },
      h(
        'div',
        { style: { ...F.row, gap: 6 } },
        h(Badge, { variant: 'warn', size: 'xs' }, 'Draft'),
        h('span', { style: T.muted }, authorLabel(m.author) + ' · ' + fmtClock(m.ts))
      ),
      editing
        ? h(Textarea, {
            value: text,
            rows: 4,
            onChange: e => setText(e.target.value),
            style: { minHeight: 80 }
          })
        : h(
            'div',
            { style: { whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 13, lineHeight: '18px' } },
            m.body
          ),
      blocked ? h('div', { style: T.warn }, blocked) : null,
      h(
        'div',
        { style: { ...F.row, gap: 6, flexWrap: 'wrap' } },
        h(
          Button,
          { size: 'xs', variant: 'default', disabled: busy || Boolean(blocked) || !text.trim(), onClick: approve },
          'Approve & send'
        ),
        editing
          ? h(
              Button,
              {
                size: 'xs',
                variant: 'outline',
                disabled: busy,
                onClick: () => {
                  setText(m.body || '')
                  setEditing(false)
                }
              },
              'Cancel edit'
            )
          : h(Button, { size: 'xs', variant: 'outline', disabled: busy, onClick: () => setEditing(true) }, 'Edit'),
        h(Button, { size: 'xs', variant: 'ghost', disabled: busy, onClick: discard }, 'Discard')
      )
    )
  )
}

function Message({ m, retried, onRetry, onMediaLoad }) {
  const out = m.direction === 'out'
  const failed = m.status === 'failed'
  const media = m.media || []
  const autoImage = media.length === 1
  const meta = []
  if (out) {
    meta.push(h('span', { key: 'a' }, authorLabel(m.author)))
  }
  meta.push(h('span', { key: 't' }, fmtClock(m.ts)))
  if (m.source === 'history') {
    meta.push(
      h('span', { key: 'h', title: 'Imported from WhatsApp history', style: { fontStyle: 'italic' } }, 'imported')
    )
  }
  if (out && m.source !== 'history' && TICK_ICONS[m.status]) {
    meta.push(h(StatusTicks, { key: 's', status: m.status, title: tickTitle(m) }))
  } else if (failed) {
    meta.push(h('span', { key: 's', style: { color: 'var(--ui-red)', fontWeight: 600 } }, 'Failed'))
  }
  return h(
    'div',
    { style: { display: 'flex', justifyContent: out ? 'flex-end' : 'flex-start', padding: '2px 0' } },
    h(
      'div',
      {
        style: {
          ...F.col,
          gap: 4,
          maxWidth: '75%',
          minWidth: 0,
          padding: '6px 10px',
          borderRadius: 10,
          border: failed ? '1px solid var(--ui-red)' : '1px solid transparent',
          background: out ? 'color-mix(in srgb, var(--ui-accent) 16%, transparent)' : 'var(--ui-bg-tertiary)',
          opacity: m.source === 'history' ? 0.85 : 1
        }
      },
      media.map(item => h(MediaItem, { key: item.index, msg: m, item, auto: autoImage, onLoad: onMediaLoad })),
      m.body
        ? h(
            'div',
            { style: { whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 13, lineHeight: '18px' } },
            m.body
          )
        : null,
      h('div', { style: { ...F.row, gap: 6, justifyContent: out ? 'flex-end' : 'flex-start', ...T.muted } }, meta),
      failed
        ? h(
            'div',
            { style: { ...F.row, gap: 6, flexWrap: 'wrap', fontSize: 11 } },
            h('span', { style: { color: 'var(--ui-red)' } }, m.error || 'Could not be sent'),
            retried
              ? h('span', { style: T.muted }, 'Retried')
              : media.length === 0 && m.body
                ? h(Button, { size: 'xs', variant: 'outline', onClick: () => onRetry(m) }, 'Retry')
                : null
          )
        : null
    )
  )
}

function OptimisticBubble({ o }) {
  return h(
    'div',
    { style: { display: 'flex', justifyContent: 'flex-end', padding: '2px 0' } },
    h(
      'div',
      {
        style: {
          ...F.col,
          gap: 4,
          maxWidth: '75%',
          padding: '6px 10px',
          borderRadius: 10,
          background: 'color-mix(in srgb, var(--ui-accent) 16%, transparent)',
          opacity: 0.8
        }
      },
      o.file ? h('div', { style: T.secondary }, '[file] ' + o.file) : null,
      o.text
        ? h(
            'div',
            { style: { whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 13, lineHeight: '18px' } },
            o.text
          )
        : null,
      h(
        'div',
        { style: { ...F.row, gap: 6, justifyContent: 'flex-end', ...T.muted } },
        h('span', null, 'You'),
        h('span', null, fmtClock(o.ts)),
        h(Codicon, { name: 'clock', title: 'Sending' })
      )
    )
  )
}

function DaySeparator({ ts }) {
  return h(
    'div',
    { style: { display: 'flex', justifyContent: 'center', padding: '10px 0 6px' } },
    h(
      'span',
      {
        style: {
          ...T.muted,
          padding: '2px 10px',
          borderRadius: 999,
          background: 'var(--ui-bg-tertiary)'
        }
      },
      fmtDay(ts)
    )
  )
}

function Thread({ id, optimistic, retried, blocked, onRetry }) {
  const latest = useApi(['messages', id], '/conversations/' + id + '/messages?limit=' + PAGE_SIZE)
  const [older, setOlder] = useState([])
  const [olderMore, setOlderMore] = useState(null)
  const [loadingOlder, setLoadingOlder] = useState(false)
  const [unseen, setUnseen] = useState(false)
  const scroller = useRef(null)
  const stick = useRef(true)
  const anchor = useRef(null)
  const lastTail = useRef(null)
  const lastOptimistic = useRef(0)

  const messages = useMemo(() => {
    const byId = new Map()
    for (const m of older) {
      byId.set(m.id, m)
    }
    for (const m of latest.data ? latest.data.messages : []) {
      byId.set(m.id, m)
    }
    return [...byId.values()].filter(m => m.status !== 'discarded').sort((a, b) => a.ts - b.ts || a.id - b.id)
  }, [older, latest.data])

  const hasMore = olderMore === null ? Boolean(latest.data && latest.data.has_more) : olderMore
  const lastId = messages.length ? messages[messages.length - 1].id : 0
  const tail = lastId + ':' + optimistic.length

  const toBottom = useCallback(() => {
    const el = scroller.current
    if (el) {
      el.scrollTop = el.scrollHeight
    }
    stick.current = true
    setUnseen(false)
  }, [])

  useLayoutEffect(() => {
    const el = scroller.current
    if (!el) {
      return
    }
    const ownSend = optimistic.length > lastOptimistic.current
    lastOptimistic.current = optimistic.length
    if (lastTail.current === null || stick.current || ownSend) {
      toBottom()
    } else if (lastTail.current !== tail) {
      setUnseen(true)
    }
    lastTail.current = tail
  }, [tail, optimistic.length, toBottom])

  // Keep the viewport anchored when older messages are prepended.
  useLayoutEffect(() => {
    const el = scroller.current
    if (el && anchor.current) {
      el.scrollTop = el.scrollHeight - anchor.current.height + anchor.current.top
      anchor.current = null
    }
  }, [older])

  const onMediaLoad = useCallback(() => {
    if (stick.current && scroller.current) {
      scroller.current.scrollTop = scroller.current.scrollHeight
    }
  }, [])

  const onScroll = e => {
    const el = e.currentTarget
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    if (stick.current) {
      setUnseen(false)
    }
  }

  const loadOlder = async () => {
    if (!messages.length) {
      return
    }
    const beforeId = Math.min(...messages.map(m => m.id))
    setLoadingOlder(true)
    try {
      const res = await ctxRef.rest('/conversations/' + id + '/messages?limit=' + PAGE_SIZE + '&before_id=' + beforeId)
      const el = scroller.current
      if (el) {
        anchor.current = { height: el.scrollHeight, top: el.scrollTop }
      }
      setOlder(prev => [...res.messages, ...prev])
      setOlderMore(res.has_more)
    } catch (err) {
      host.notify({ kind: 'error', title: 'Could not load older messages', message: failureText(err) })
    } finally {
      setLoadingOlder(false)
    }
  }

  if (!latest.data) {
    return h(
      'div',
      { style: { flex: '1 1 auto', minHeight: 0, padding: 12 } },
      latest.error
        ? h(ErrorState, { title: 'Backend unreachable', description: errorText(latest.error) })
        : h(Skeleton, { className: 'h-full w-full' })
    )
  }

  const items = []
  let lastDay = ''
  for (const m of messages) {
    const dk = dayKey(m.ts)
    if (dk !== lastDay) {
      items.push(h(DaySeparator, { key: 'd' + dk, ts: m.ts }))
      lastDay = dk
    }
    items.push(
      m.status === 'draft' && m.direction === 'out'
        ? h(DraftCard, { key: m.id, m, blocked })
        : h(Message, {
            key: m.id,
            m,
            retried: retried.includes(m.id),
            onRetry,
            onMediaLoad
          })
    )
  }

  return h(
    'div',
    { style: { position: 'relative', flex: '1 1 auto', minHeight: 0, ...F.col } },
    latest.error
      ? h('div', { style: { ...T.warn, padding: '4px 12px' } }, 'Data may be stale: ' + errorText(latest.error))
      : null,
    h(
      'div',
      { ref: scroller, onScroll, style: { flex: '1 1 auto', minHeight: 0, overflowY: 'auto', padding: '8px 14px' } },
      hasMore
        ? h(
            'div',
            { style: { textAlign: 'center', padding: '4px 0 8px' } },
            h(Button, { size: 'xs', variant: 'outline', loading: loadingOlder, onClick: loadOlder }, 'Load older')
          )
        : null,
      messages.length === 0 && optimistic.length === 0
        ? h('div', { style: { ...T.muted, textAlign: 'center', padding: 24 } }, 'No messages yet')
        : null,
      items,
      optimistic.map(o => h(OptimisticBubble, { key: o.key, o }))
    ),
    unseen
      ? h(
          'div',
          { style: { position: 'absolute', left: 0, right: 0, bottom: 10, display: 'flex', justifyContent: 'center' } },
          h(
            Button,
            { size: 'xs', variant: 'default', onClick: toBottom },
            h(Codicon, { name: 'arrow-down' }),
            'New messages'
          )
        )
      : null
  )
}

// --- Composer ---------------------------------------------------------------------

function Composer({ id, blocked, maxMb, onSend, onSendMedia, onDraft }) {
  const [text, setText] = useState(() => composerText.get(id) || '')
  const [file, setFile] = useState(null)
  const [busy, setBusy] = useState(false)
  const wrap = useRef(null)
  const picker = useRef(null)

  useEffect(() => {
    const el = wrap.current ? wrap.current.querySelector('textarea') : null
    if (el) {
      el.style.height = 'auto'
      el.style.height = Math.min(el.scrollHeight, 160) + 'px'
    }
  }, [text])

  const change = value => {
    composerText.set(id, value)
    setText(value)
  }

  const submit = async () => {
    const t = text.trim()
    if (busy || blocked || (!t && !file)) {
      return
    }
    const f = file
    setBusy(true)
    change('')
    setFile(null)
    if (f) {
      await onSendMedia(f, t)
    } else {
      await onSend(t)
    }
    setBusy(false)
  }

  const saveDraft = async () => {
    const t = text.trim()
    if (!t) {
      return
    }
    change('')
    await onDraft(t)
  }

  const pick = e => {
    const f = e.target.files && e.target.files[0]
    e.target.value = ''
    if (!f) {
      return
    }
    if (f.size > maxMb * 1048576) {
      host.notify({ kind: 'error', title: 'File too large', message: 'Maximum upload size is ' + maxMb + ' MB' })
      return
    }
    readFile(f).then(setFile, err =>
      host.notify({ kind: 'error', title: 'Could not read file', message: errorText(err) })
    )
  }

  return h(
    'div',
    { style: { ...F.col, gap: 6, padding: '8px 12px 10px', borderTop: BORDER } },
    blocked ? h('div', { style: T.warn }, blocked) : null,
    file
      ? h(
          'div',
          { style: { ...F.row, gap: 6 } },
          h(Badge, { variant: 'outline' }, h(Codicon, { name: 'attach' }), file.name + ' (' + fmtSize(file.size) + ')'),
          h(Button, { size: 'xs', variant: 'ghost', onClick: () => setFile(null) }, 'Remove')
        )
      : null,
    h(
      'div',
      { ref: wrap },
      h(Textarea, {
        rows: 1,
        value: text,
        placeholder: file ? 'Caption (optional)…' : 'Write a reply…  Enter to send, Shift+Enter for a new line',
        style: { minHeight: 36, maxHeight: 160, resize: 'none' },
        onChange: e => change(e.target.value),
        onKeyDown: e => {
          if (isSubmitEnter(e) && !e.shiftKey) {
            e.preventDefault()
            submit()
          }
        }
      })
    ),
    h(
      'div',
      { style: { ...F.row, gap: 6 } },
      h('input', { ref: picker, type: 'file', hidden: true, onChange: pick }),
      h(
        Button,
        {
          size: 'xs',
          variant: 'ghost',
          disabled: Boolean(blocked) || busy,
          onClick: () => picker.current && picker.current.click()
        },
        h(Codicon, { name: 'attach' }),
        'Attach'
      ),
      h(Button, { size: 'xs', variant: 'ghost', disabled: !text.trim(), onClick: saveDraft }, 'Save as draft'),
      h('span', { style: { flex: '1 1 auto' } }),
      h(
        Button,
        {
          size: 'sm',
          variant: 'default',
          disabled: Boolean(blocked) || busy || (!text.trim() && !file),
          onClick: submit
        },
        h(Codicon, { name: 'send' }),
        'Send'
      )
    )
  )
}

// --- Header, tags, info panel ---------------------------------------------------------

function TagEditor({ c }) {
  const [value, setValue] = useState('')
  const save = tags => act('/conversations/' + c.id + '/tags', { tags }, 'PUT')
  const add = () => {
    const t = value.trim().slice(0, 40)
    setValue('')
    if (t && !c.tags.includes(t)) {
      save([...c.tags, t])
    }
  }
  return h(
    'div',
    { style: { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 4 } },
    h(Codicon, { name: 'tag', style: T.muted }),
    c.tags.map(t =>
      h(
        Badge,
        { key: t, variant: 'muted', style: { gap: 4 } },
        t,
        h(
          'button',
          {
            type: 'button',
            title: 'Remove tag',
            style: { cursor: 'pointer', background: 'transparent', color: 'inherit' },
            onClick: () => save(c.tags.filter(x => x !== t))
          },
          '×'
        )
      )
    ),
    h('input', {
      value,
      placeholder: 'Add tag…',
      onChange: e => setValue(e.target.value),
      onKeyDown: e => {
        if (isSubmitEnter(e)) {
          e.preventDefault()
          add()
        }
      },
      style: {
        width: 90,
        fontSize: 11,
        padding: '1px 4px',
        background: 'transparent',
        color: 'var(--ui-text-primary)',
        borderBottom: BORDER,
        outline: 'none'
      }
    })
  )
}

function ChatHeader({ data, settings, infoOpen, onToggleInfo }) {
  const c = data.conversation
  const allowed = data.allowed_states || []
  const base = '/conversations/' + c.id
  const presets = (settings && settings.board && settings.board.mute_presets_hours) || [1, 8, 24, 168]
  const moveItems = allowed
    .filter(s => s !== 'muted')
    .map(s => ({ key: s, label: stateLabel(s), onClick: () => act(base + '/state', { state: s }) }))
  const muteItems = presets.map(hrs => ({
    key: hrs,
    label: 'Mute for ' + fmtHours(hrs),
    onClick: () => act(base + '/mute', { muted_until: nowSec() + hrs * 3600 })
  }))
  return h(
    'div',
    { style: { ...F.col, gap: 6, padding: '8px 12px', borderBottom: BORDER } },
    h(
      'div',
      { style: { ...F.row, gap: 10, flexWrap: 'wrap' } },
      h(Avatar, { name: c.contact_name || '', size: 34 }),
      h(
        'div',
        { style: { ...F.col, minWidth: 0, flex: '1 1 160px' } },
        h('div', { style: { ...F.ellipsis, fontSize: 14, fontWeight: 600 } }, displayName(c)),
        h(
          'div',
          { style: { ...F.row, gap: 6, ...T.muted, ...F.ellipsis } },
          c.contact_name && c.phone ? h('span', null, fmtPhone(c.phone)) : null,
          h(AccountDot, { account: data.account }),
          h('span', null, data.account.label)
        )
      ),
      h(Badge, { variant: STATE_VARIANT[c.state] || 'outline' }, stateLabel(c.state)),
      c.state === 'muted' && c.muted_until ? h(Badge, { variant: 'muted' }, 'until ' + fmtTime(c.muted_until)) : null,
      c.priority >= 2 ? h(Badge, { variant: 'destructive' }, 'Escalated') : null,
      !c.agent_active ? h(Badge, { variant: 'warn' }, 'Human') : null,
      h(
        'div',
        { style: { ...F.row, gap: 4, flexWrap: 'wrap' } },
        h(Menu, { label: 'Move to', items: moveItems, disabled: moveItems.length === 0 }),
        h(Menu, { label: 'Mute', items: muteItems, disabled: !allowed.includes('muted') }),
        c.agent_active
          ? h(Button, { size: 'xs', variant: 'secondary', onClick: () => act(base + '/takeover', {}) }, 'Take over')
          : h(
              Button,
              { size: 'xs', variant: 'secondary', onClick: () => act(base + '/handback', {}) },
              'Hand back to agent'
            ),
        h(
          Button,
          {
            size: 'xs',
            variant: c.priority >= 2 ? 'outline' : 'secondary',
            onClick: () => act(base + '/escalate', { escalated: c.priority < 2 })
          },
          c.priority >= 2 ? 'De-escalate' : 'Escalate'
        ),
        h(
          Button,
          {
            size: 'xs',
            variant: infoOpen ? 'outline' : 'ghost',
            title: 'Toggle details (Esc closes)',
            onClick: onToggleInfo
          },
          h(Codicon, { name: 'info' }),
          'Info'
        )
      )
    ),
    h(TagEditor, { c })
  )
}

const RUN_VARIANT = { queued: 'muted', running: 'default', done: 'success', failed: 'destructive', skipped: 'outline' }

function InfoPanel({ data, onClose }) {
  const c = data.conversation
  const account = data.account
  const history = data.state_history || []
  const runs = data.runs || []
  return h(
    'div',
    { style: { ...F.col, width: '19rem', flex: '0 0 auto', minHeight: 0, overflowY: 'auto', borderLeft: BORDER } },
    h(
      'div',
      { style: { ...F.row, justifyContent: 'space-between', padding: '8px 12px', borderBottom: BORDER } },
      h('span', { style: { fontWeight: 600 } }, 'Details'),
      h(Button, { size: 'xs', variant: 'ghost', title: 'Close (Esc)', onClick: onClose }, '✕')
    ),
    h(
      Section,
      { title: 'Contact' },
      h(KV, { label: 'Name' }, c.contact_name || '—'),
      h(KV, { label: 'Phone' }, fmtPhone(c.phone) || '—'),
      h(KV, { label: 'Chat id' }, c.chat_jid),
      h(
        KV,
        { label: 'Account' },
        h(AccountDot, { account }),
        ' ' + account.label + (account.phone ? ' · ' + fmtPhone(account.phone) : '')
      ),
      h(
        KV,
        { label: 'State' },
        stateLabel(c.state) + (c.previous_state ? ' (was ' + stateLabel(c.previous_state) + ')' : '')
      ),
      c.muted_until ? h(KV, { label: 'Muted until' }, fmtTime(c.muted_until)) : null,
      h(KV, { label: 'Handled by' }, c.agent_active ? 'Agent' : 'Human'),
      h(KV, { label: 'Priority' }, c.priority >= 2 ? 'Escalated' : 'Normal'),
      h(KV, { label: 'Started' }, c.created_at ? fmtTime(c.created_at) : '—'),
      h(KV, { label: 'Last message' }, c.last_message_at ? fmtTime(c.last_message_at) : '—')
    ),
    h(
      Section,
      { title: 'State history' },
      history.length === 0 ? h('div', { style: T.muted }, 'No transitions') : null,
      history.map(r =>
        h(
          'div',
          { key: r.id, style: { fontSize: 12 } },
          (r.from_state ? stateLabel(r.from_state) : '—') + ' → ' + stateLabel(r.to_state),
          h('div', { style: T.muted }, [r.actor, r.reason, fmtTime(r.at)].filter(Boolean).join(' · '))
        )
      )
    ),
    h(
      Section,
      { title: 'Automation runs' },
      runs.length === 0 ? h('div', { style: T.muted }, 'No automation runs') : null,
      runs.map(r =>
        h(
          'div',
          { key: r.id, style: { ...F.col, gap: 2, fontSize: 12 } },
          h(
            'div',
            { style: { ...F.row, gap: 6, flexWrap: 'wrap' } },
            h(Badge, { variant: RUN_VARIANT[r.status] || 'outline', size: 'xs' }, r.status),
            h('span', null, r.rule_name || 'Rule #' + r.rule_id),
            r.status === 'failed'
              ? h(
                  Button,
                  { size: 'xs', variant: 'outline', onClick: () => act('/automation-runs/' + r.id + '/retry', {}) },
                  'Retry'
                )
              : null
          ),
          h('div', { style: T.muted }, fmtTime(r.finished_at || r.started_at || r.created_at)),
          r.error ? h('div', { style: { color: 'var(--ui-red)', wordBreak: 'break-word' } }, r.error) : null
        )
      )
    )
  )
}

// --- Chat pane -------------------------------------------------------------------------

function ChatPane({ id, infoOpen, onToggleInfo, onCloseInfo }) {
  const detail = useApi(['conv', id], '/conversations/' + id)
  const settings = useApi('settings', '/settings')
  const service = useApi('service', '/service')
  const [optimistic, setOptimistic] = useState([])
  const [retried, setRetried] = useState([])
  const seq = useRef(0)
  const readKey = useRef('')

  const conv = detail.data ? detail.data.conversation : null
  const unread = conv ? conv.unread_count : 0
  const loaded = Boolean(conv)

  // Mark read on open and whenever new inbound arrives while open.
  useEffect(() => {
    if (!loaded || !unread) {
      return
    }
    const key = id + ':' + unread
    if (readKey.current === key) {
      return
    }
    readKey.current = key
    rest('/conversations/' + id + '/read', { method: 'POST', body: {} }).then(refresh, () => {})
  }, [id, loaded, unread])

  const withOptimistic = async (entry, run) => {
    const temp = { key: 'o' + ++seq.current, ts: nowSec(), ...entry }
    setOptimistic(o => [...o, temp])
    const res = await run()
    setOptimistic(o => o.filter(x => x !== temp))
    return Boolean(res)
  }

  const sendText = text =>
    withOptimistic({ text }, () => call('/conversations/' + id + '/reply', { text }, 'POST', 'Send failed'))

  const sendMedia = (file, caption) =>
    withOptimistic({ text: caption, file: file.name }, () =>
      call(
        '/conversations/' + id + '/reply-media',
        { filename: file.name, mime: file.mime, data_base64: file.data_base64, caption },
        'POST',
        'Send failed'
      )
    )

  const saveDraft = text => act('/conversations/' + id + '/drafts', { text })

  const retry = async m => {
    setRetried(r => [...r, m.id])
    const ok = await sendText(m.body)
    if (!ok) {
      setRetried(r => r.filter(x => x !== m.id))
    }
  }

  if (!detail.data) {
    return h(
      'div',
      { style: { ...F.col, ...F.fill, padding: 12 } },
      detail.error
        ? h(ErrorState, { title: 'Backend unreachable', description: errorText(detail.error) })
        : h(Skeleton, { className: 'h-20 w-full' })
    )
  }

  const blocked = sendBlock(detail.data, service.data)
  const maxMb = (settings.data && settings.data.media && settings.data.media.max_upload_mb) || 15

  return h(
    'div',
    { style: { display: 'flex', flex: '1 1 auto', minWidth: 0, minHeight: 0 } },
    h(
      'div',
      { style: { ...F.col, ...F.fill } },
      h(ChatHeader, { data: detail.data, settings: settings.data, infoOpen, onToggleInfo }),
      detail.error
        ? h('div', { style: { ...T.warn, padding: '4px 12px' } }, 'Data may be stale: ' + errorText(detail.error))
        : null,
      h(Thread, { id, optimistic, retried, blocked, onRetry: retry }),
      h(Composer, { key: id, id, blocked, maxMb, onSend: sendText, onSendMedia: sendMedia, onDraft: saveDraft })
    ),
    infoOpen ? h(InfoPanel, { data: detail.data, onClose: onCloseInfo }) : null
  )
}

// --- Board tab ---------------------------------------------------------------------------

function BoardCard({ card, onOpen }) {
  return h(
    'div',
    {
      draggable: true,
      onDragStart: e => e.dataTransfer.setData('text/plain', String(card.id)),
      onClick: () => onOpen(card.id),
      style: {
        ...F.col,
        gap: 4,
        padding: 8,
        cursor: 'pointer',
        fontSize: 12,
        borderRadius: 6,
        border: BORDER,
        borderLeft: '3px solid ' + URGENCY_COLOR[card.urgency || 'normal']
      }
    },
    h(
      'div',
      { style: { ...F.row, gap: 6 } },
      h(AccountDot, { account: card }),
      h('span', { style: { ...F.ellipsis, flex: '1 1 auto', fontWeight: 600 } }, displayName(card)),
      card.unread_count > 0 ? h(Badge, { variant: 'solid', size: 'xs' }, String(card.unread_count)) : null
    ),
    h(
      'div',
      {
        style: {
          ...T.secondary,
          display: '-webkit-box',
          WebkitLineClamp: 2,
          WebkitBoxOrient: 'vertical',
          overflow: 'hidden'
        }
      },
      card.has_draft ? h('span', { style: { color: 'var(--ui-orange)', fontWeight: 600 } }, 'Draft · ') : null,
      previewPrefix(card) + (card.last_message_preview || '')
    ),
    h(
      'div',
      { style: { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 4, ...T.muted } },
      h('span', null, fmtAge(card.age_seconds)),
      card.priority >= 2 ? h(Badge, { variant: 'destructive', size: 'xs' }, 'Escalated') : null,
      !card.agent_active ? h(Badge, { variant: 'warn', size: 'xs' }, 'Human') : null,
      card.state === 'muted' && card.muted_until ? h('span', null, 'until ' + fmtTime(card.muted_until)) : null,
      (card.tags || []).map(t => h(Badge, { key: t, variant: 'muted', size: 'xs' }, t))
    )
  )
}

function BoardColumn({ col, count, hidden, onOpen, onDrop }) {
  const [over, setOver] = useState(false)
  return h(
    'div',
    {
      onDragOver: e => {
        e.preventDefault()
        setOver(true)
      },
      onDragLeave: () => setOver(false),
      onDrop: e => {
        e.preventDefault()
        setOver(false)
        const cardId = Number(e.dataTransfer.getData('text/plain'))
        if (cardId) {
          onDrop(col, cardId)
        }
      },
      style: {
        ...F.col,
        gap: 8,
        width: '17rem',
        flex: '0 0 auto',
        minHeight: 0,
        padding: 8,
        borderRadius: 8,
        border: over ? '1px dashed var(--ui-accent)' : '1px solid transparent',
        background: over ? 'color-mix(in srgb, var(--ui-accent) 8%, transparent)' : 'var(--ui-bg-secondary)'
      }
    },
    h(
      'div',
      { style: { ...F.row, fontSize: 12, fontWeight: 600 } },
      h('span', null, stateLabel(col.name)),
      h(Badge, { variant: 'muted' }, String(count))
    ),
    hidden
      ? h('div', { style: T.muted }, 'Hidden. Enable "Show closed" to list them; drop a card here to close it.')
      : h(
          'div',
          { style: { ...F.col, gap: 6, minHeight: 0, overflowY: 'auto' } },
          col.cards.map(card => h(BoardCard, { key: card.id, card, onOpen }))
        )
  )
}

function BoardView({ accountId, onOpen }) {
  const [includeClosed, setIncludeClosed] = useState(false)
  const [qInput, setQInput] = useState('')
  const [q, setQ] = useState('')
  const settings = useApi('settings', '/settings')

  useEffect(() => ctxRef.setTimeout(() => setQ(qInput.trim()), 250), [qInput])

  const qs = new URLSearchParams({ include_closed: String(includeClosed) })
  if (accountId) {
    qs.set('account_id', String(accountId))
  }
  if (q) {
    qs.set('q', q)
  }
  const board = useApi('board', '/board?' + qs)
  const dropHours = (settings.data && settings.data.board && settings.data.board.drop_mute_hours) || 24

  const onDrop = (col, cardId) => {
    if (col.cards.some(c => c.id === cardId)) {
      return
    }
    if (col.name === 'muted') {
      act('/conversations/' + cardId + '/mute', { muted_until: nowSec() + dropHours * 3600 })
    } else {
      act('/conversations/' + cardId + '/state', { state: col.name })
    }
  }

  return h(
    'div',
    { style: { ...F.col, ...F.fill, gap: 8, padding: 12 } },
    h(
      'div',
      { style: { ...F.row, flexWrap: 'wrap' } },
      h(Input, {
        value: qInput,
        placeholder: 'Search name or phone',
        onChange: e => setQInput(e.target.value),
        style: { width: '16rem' }
      }),
      h(
        Button,
        { size: 'xs', variant: includeClosed ? 'secondary' : 'outline', onClick: () => setIncludeClosed(v => !v) },
        includeClosed ? 'Hide closed' : 'Show closed'
      ),
      h('span', { style: T.muted }, 'Dropping a card on Muted snoozes it for ' + fmtHours(dropHours))
    ),
    gate(board, data =>
      h(
        'div',
        {
          style: { display: 'flex', gap: 12, flex: '1 1 auto', minHeight: 0, overflowX: 'auto', alignItems: 'stretch' }
        },
        data.columns.map(col =>
          h(BoardColumn, {
            key: col.name,
            col,
            count: col.name === 'closed' && !includeClosed ? (data.counts || {}).closed || 0 : col.cards.length,
            hidden: col.name === 'closed' && !includeClosed,
            onOpen,
            onDrop
          })
        )
      )
    )
  )
}

// --- Page ----------------------------------------------------------------------------------

function ChatsView({ accountId, selected, onSelect }) {
  const [stateFilter, setStateFilter] = useState('')
  const [infoOpen, setInfoOpen] = useState(() => Boolean(ctxRef.storage.get('infoOpen', false)))

  const setInfo = useCallback(value => {
    setInfoOpen(value)
    ctxRef.storage.set('infoOpen', value)
  }, [])

  // Esc closes the details panel.
  useEffect(() => {
    if (!infoOpen) {
      return undefined
    }
    return ctxRef.addEventListener(window, 'keydown', e => {
      if (e.key === 'Escape') {
        setInfo(false)
      }
    })
  }, [infoOpen, setInfo])

  return h(
    'div',
    { style: { display: 'flex', ...F.fill } },
    h(ConvSidebar, { accountId, stateFilter, setStateFilter, selectedId: selected, onSelect }),
    selected
      ? h(ChatPane, {
          key: selected,
          id: selected,
          infoOpen,
          onToggleInfo: () => setInfo(!infoOpen),
          onCloseInfo: () => setInfo(false)
        })
      : h(
          'div',
          { style: { ...F.col, ...F.fill, alignItems: 'center', justifyContent: 'center', gap: 6 } },
          h(Codicon, { name: 'comment-discussion', size: '2rem', style: T.muted }),
          h('div', { style: T.muted }, 'Select a conversation')
        )
  )
}

// Header number switcher (Kanban board-switcher style): "All numbers" or one WhatsApp number.
function NumberSwitcher({ scope }) {
  const account = useValue(scope.account)
  const accounts = useApi('accounts', '/accounts')
  const list = useMemo(
    () => ((accounts.data && accounts.data.accounts) || []).filter(a => a.desired !== 'removed'),
    [accounts.data]
  )
  const current = list.find(a => a.id === account) || null

  // The selected number was removed: fall back to "All numbers".
  useEffect(() => {
    if (account !== null && accounts.data && !current) {
      scope.account.set(null)
    }
  }, [account, accounts.data, current, scope])

  const label = current ? current.label : 'All numbers'
  const choose = id => {
    scope.account.set(id)
    scope.selected.set(null)
  }

  return h(
    DropdownMenu,
    null,
    h(
      DropdownMenuTrigger,
      { asChild: true },
      h(
        Button,
        {
          'aria-label': 'WhatsApp number: ' + label,
          className: 'h-full min-w-0 max-w-full gap-1.5 px-2',
          size: 'sm',
          variant: 'ghost'
        },
        h(Codicon, { name: 'device-mobile', size: '0.8125rem' }),
        current ? h(AccountDot, { account: current }) : null,
        h('span', { style: { ...F.ellipsis, fontSize: 12, fontWeight: 500 } }, label),
        h(Codicon, { name: 'chevron-down', size: '0.8125rem' })
      )
    ),
    h(
      DropdownMenuContent,
      { align: 'center' },
      h(
        DropdownMenuItem,
        { onSelect: () => choose(null) },
        'All numbers',
        account === null ? h(Codicon, { className: 'ml-auto', name: 'check', size: '0.8rem' }) : null
      ),
      list.map(a => {
        const st = accountState(a)
        return h(
          DropdownMenuItem,
          { key: a.id, onSelect: () => choose(a.id) },
          h(AccountDot, { account: a }),
          a.label,
          st === 'connected' || st === 'demo'
            ? null
            : h('span', { style: { color: accountStateColor(st), fontSize: 10 } }, ACCOUNT_STATE_LABELS[st] || st),
          a.id === account ? h(Codicon, { className: 'ml-auto', name: 'check', size: '0.8rem' }) : null
        )
      })
    )
  )
}

function WaPage({ route, accountId }) {
  const scope = getScope(route, accountId)
  const tab = useValue(scope.tab)
  const selected = useValue(scope.selected)
  const account = useValue(scope.account)
  const health = useApi('service', '/service')
  const accounts = useApi('accounts', '/accounts')
  const open = useCallback(
    id => {
      scope.selected.set(id)
      scope.tab.set('chats')
    },
    [scope]
  )

  const unreachable = !health.data && !accounts.data && (health.error || accounts.error)

  return h(
    'div',
    { style: { ...F.col, height: '100%', minWidth: 0, minHeight: 0 } },
    h(
      'div',
      { style: { ...F.row, padding: '8px 12px', borderBottom: BORDER } },
      h(WorkspacePageHeaderControl, { id: 'hermes-whatsapp-chat:number-switcher' }, h(NumberSwitcher, { scope })),
      h(SegmentedControl, {
        options: [
          { id: 'chats', label: 'Chats' },
          { id: 'board', label: 'Board' },
          { id: 'settings', label: 'Settings' }
        ],
        value: tab,
        onChange: id => scope.tab.set(id)
      }),
      h('span', { style: { flex: '1 1 auto' } }),
      unreachable
        ? h('span', { style: T.warn }, 'Backend unreachable: ' + errorText(health.error || accounts.error))
        : null,
      h(
        Button,
        { size: 'xs', variant: 'ghost', title: 'Refresh', onClick: () => refresh() },
        h(Codicon, { name: 'refresh' })
      )
    ),
    h(OutdatedBanner, {}),
    h(ServiceBanner, {}),
    tab === 'chats' ? h(ChatsView, { accountId: account, selected, onSelect: open }) : null,
    tab === 'board' ? h(BoardView, { accountId: account, onOpen: open }) : null,
    tab === 'settings' ? h('div', { style: { ...F.fill, overflowY: 'auto' } }, jsx(SettingsPage, {})) : null
  )
}

// Statusbar chip: unanswered "new" count + age of the oldest, plus a warning when
// the service is down or an account is not connected.
function StatusChip() {
  const news = useApi('chip-new', '/conversations?state=new&limit=100')
  const accounts = useApi('accounts', '/accounts')
  const service = useApi('service', '/service')

  if (!news.data && !news.error) {
    return null
  }

  const serviceDown = Boolean(service.data && !service.data.running)
  const issues = (accounts.data ? accounts.data.accounts : []).filter(
    a => a.kind !== 'demo' && a.desired === 'running' && accountState(a) !== 'connected'
  )
  let text = 'WA ?'
  let oldest = ''
  if (news.data) {
    oldest = fmtAge(Math.max(0, ...news.data.conversations.map(c => c.age_seconds || 0)))
    text = 'WA ' + news.data.total + (news.data.total > 0 && oldest ? ' · ' + oldest : '')
  }
  const warn =
    news.error && !news.data ? 'unreachable' : serviceDown ? 'service down' : issues.length ? 'not connected' : ''
  const tone = news.error && !news.data ? 'bad' : serviceDown ? 'bad' : issues.length ? 'warn' : 'good'
  return h(
    'button',
    {
      type: 'button',
      className: 'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem]',
      style: T.muted,
      title: warn ? 'WhatsApp: ' + warn : 'Conversations',
      onClick: () => {
        haptic('tap')
        host.navigate(ROUTE)
      }
    },
    h(StatusDot, { tone }),
    h('span', null, text),
    warn ? h('span', { style: { color: tone === 'bad' ? 'var(--ui-red)' : 'var(--ui-orange)' } }, '· ' + warn) : null
  )
}

// --- Live events ------------------------------------------------------------------------------

let settingsCache = null

async function getSettings() {
  if (settingsCache && Date.now() - settingsCache.at < 30000) {
    return settingsCache.value
  }
  const value = await ctxRef.rest('/settings')
  settingsCache = { at: Date.now(), value }
  return value
}

function notifyConv(title, e, body) {
  ctxRef.os.notify({
    title,
    body,
    activate: ROUTE,
    onActivate: () => {
      const scope = getScope(ROUTE, null)
      scope.account.set(null)
      if (e.conversation_id) {
        scope.selected.set(e.conversation_id)
      }
      scope.tab.set('chats')
    }
  })
}

async function notifyEvents(events) {
  if (events.some(e => e.type === 'settings.updated')) {
    settingsCache = null
  }
  let settings
  try {
    settings = await getSettings()
  } catch {
    return
  }
  const n = (settings && settings.notifications) || {}
  if (inQuietHours(n.quiet_hours, new Date())) {
    return
  }
  const cutoff = nowSec() - 120
  const created = new Set()
  for (const e of events) {
    if (e.at && e.at < cutoff) {
      continue
    }
    const payload = parsePayload(e.payload)
    const who = e.contact_name || 'Unknown contact'
    if (e.type === 'conversation.created') {
      created.add(e.conversation_id)
      if (n.new_conversation) {
        notifyConv('New conversation', e, who)
      }
    } else if (e.type === 'message.in') {
      if (n.every_inbound && !created.has(e.conversation_id)) {
        notifyConv(who, e, payload.preview || payload.body || payload.text || 'New message')
      }
    } else if (e.type === 'conversation.updated' && n.escalation) {
      const fields = Array.isArray(payload.fields) ? payload.fields : []
      if (!fields.includes('priority') && payload.escalated === undefined) {
        continue
      }
      let escalated = payload.escalated
      if (typeof escalated !== 'boolean') {
        try {
          const d = await ctxRef.rest('/conversations/' + e.conversation_id)
          escalated = d.conversation.priority >= 2
        } catch {
          escalated = false
        }
      }
      if (escalated) {
        notifyConv('Escalated: ' + who, e, 'This conversation needs a human')
      }
    }
  }
}

function onFrame(frame) {
  refresh()
  const events = frame && Array.isArray(frame.events) ? frame.events : []
  if (events.length) {
    notifyEvents(events)
  }
  if (events.some(e => typeof e.type === 'string' && e.type.startsWith('account.'))) {
    loadNumberRoutes()
  }
}

// --- Per-number routes ------------------------------------------------------------------------

// One page route + one sidebar entry per WhatsApp number (`/wa-board-<account id>`). Registered at
// runtime from /accounts and re-registered only when the list of numbers changes.
let numberRoutes = { key: '', dispose: null }

function syncNumberRoutes(accounts) {
  const list = accounts
    .filter(a => a.kind === 'whatsapp' && a.desired !== 'removed')
    .map(a => ({ id: a.id, label: a.label }))
  const key = JSON.stringify(list)
  if (key === numberRoutes.key) {
    return
  }
  if (numberRoutes.dispose) {
    numberRoutes.dispose()
  }
  const contributions = []
  for (const n of list) {
    const path = ROUTE + '-' + n.id
    contributions.push(
      {
        id: 'page-' + n.id,
        area: ROUTES_AREA,
        title: 'WhatsApp · ' + n.label,
        data: { path },
        render: () => jsx(WaPage, { route: path, accountId: n.id })
      },
      {
        id: 'nav-' + n.id,
        area: SIDEBAR_NAV_AREA,
        data: { path, label: 'WhatsApp · ' + n.label, codicon: 'device-mobile' }
      }
    )
  }
  numberRoutes = { key, dispose: contributions.length ? ctxRef.registerMany(contributions) : null }
}

function loadNumberRoutes() {
  return ctxRef.rest('/accounts').then(
    d => syncNumberRoutes((d && d.accounts) || []),
    () => {}
  )
}

// --- Register ------------------------------------------------------------------------------------

export { fmtAge, fmtHours, errorText, initials, inQuietHours }

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
        render: () => jsx(WaPage, { route: ROUTE, accountId: null })
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
          keywords: ['wa', 'whatsapp', 'board', 'conversations', 'chat'],
          run: () => host.navigate(ROUTE)
        }
      },
      { id: 'chip', area: STATUSBAR_AREAS.right, order: 140, render: () => jsx(StatusChip, {}) }
    ])

    // Live updates: an accelerator over the 5s polling (a no-op on OAuth remotes).
    ctx.socket('/events', onFrame)

    // One route + sidebar entry per WhatsApp number.
    numberRoutes = { key: '', dispose: null }
    loadNumberRoutes()
    ctx.setInterval(loadNumberRoutes, 15000)
  }
}

// --- Settings page (pasted from settings_fragment.js) ---

// --- Settings page (fragment pasted into plugin.js) ---------------------------
// Relies on host-module scope: jsx, jsxs, useState, useEffect, Badge, Button, ErrorState, Input,
// Skeleton, Textarea, host, queryClient, useQuery, ID, rest, act, errorText, fmtAge, fmtTime,
// refresh, useApi, STATE_LABELS, ACCOUNT_COLORS, AccountDot, ServiceBanner.

const SETTINGS_SECTIONS = [
  ['numbers', 'Numbers'],
  ['rules', 'Conversation rules'],
  ['notifications', 'Notifications'],
  ['privacy', 'Privacy'],
  ['board', 'Board'],
  ['hours', 'Hours'],
  ['automations', 'Automations']
]
const SETTINGS_SAVE_SECTIONS = ['rules', 'notifications', 'privacy', 'board', 'hours', 'automations']

// state -> [label, Badge variant]
const SETTINGS_STATE_BADGE = {
  connected: ['Connected', 'success'],
  qr: ['Scan QR', 'warn'],
  pairing: ['Pairing', 'warn'],
  connecting: ['Connecting', 'warn'],
  starting: ['Starting', 'warn'],
  disconnected: ['Disconnected', 'destructive'],
  error: ['Error', 'destructive'],
  logged_out: ['Logged out', 'destructive'],
  stopped: ['Stopped', 'muted']
}
const SETTINGS_HISTORY_MODES = [
  ['off', 'Off', 'Only messages received from now on.'],
  ['recent', 'Recent', 'Import the recent chat history WhatsApp sends after linking.'],
  ['full', 'Full', 'Import as much history as WhatsApp provides after linking.']
]
const SETTINGS_WEEKDAYS = [
  ['mon', 'Monday'],
  ['tue', 'Tuesday'],
  ['wed', 'Wednesday'],
  ['thu', 'Thursday'],
  ['fri', 'Friday'],
  ['sat', 'Saturday'],
  ['sun', 'Sunday']
]
const SETTINGS_EVENT_TYPES = [
  ['message.in', 'Message received'],
  ['message.out', 'Message sent'],
  ['conversation.created', 'Conversation created'],
  ['conversation.state_changed', 'State changed']
]
const SETTINGS_STATES = ['new', 'in_progress', 'waiting', 'muted', 'closed']
const SETTINGS_ACTIONS = [
  ['hermes', 'Hermes agent / profile'],
  ['webhook', 'Webhook'],
  ['script', 'Script'],
  ['reply', 'Fixed reply'],
  ['set_state', 'Set state'],
  ['add_tags', 'Add tags'],
  ['escalate', 'Escalate'],
  ['takeover', 'Take over']
]
const SETTINGS_REPLY_ACTIONS = ['hermes', 'webhook', 'script', 'reply']
const SETTINGS_TIMEOUTS = { hermes: 180, webhook: 30, script: 60 }
const SETTINGS_TEMPLATE_VARS = [
  ['{contact_name}', 'contact name'],
  ['{phone}', 'contact phone number'],
  ['{text}', 'text of the triggering message'],
  ['{account_label}', 'label of the number'],
  ['{state}', 'conversation state'],
  ['{conversation_id}', 'conversation id'],
  ['{history}', 'last messages as "[time] author: body" lines'],
  ['{wa_cli}', 'absolute command to run the wa.py CLI']
]
const SETTINGS_REPLY_MODES = [
  ['draft', 'Draft', 'The reply is saved as a draft you approve from the chat.'],
  ['send', 'Send', 'The reply is sent to the contact immediately.'],
  ['none', 'None', 'The reply is only stored in the run output.']
]
const SETTINGS_DEFAULT_PROMPT =
  'You are replying on WhatsApp on behalf of the account owner.\n' +
  'Contact: {contact_name} ({phone}) on {account_label}\n' +
  'Conversation state: {state}\n\n' +
  'Recent messages:\n{history}\n\n' +
  'Latest message: {text}\n\n' +
  'Write only the reply text.'
const SETTINGS_RUN_BADGE = {
  queued: 'muted',
  running: 'warn',
  done: 'success',
  failed: 'destructive',
  skipped: 'outline'
}

const set_muted = { color: 'var(--ui-text-tertiary)' }
const set_secondary = { color: 'var(--ui-text-secondary)' }
const set_card = {
  border: '1px solid var(--ui-stroke-secondary)',
  borderRadius: 6,
  padding: 12
}
const set_subtle = {
  border: '1px solid var(--ui-stroke-secondary)',
  borderRadius: 6,
  padding: 10,
  background: 'var(--ui-bg-quaternary)'
}
const set_errorBox = {
  border: '1px solid var(--ui-red)',
  borderRadius: 6,
  padding: '6px 10px',
  color: 'var(--ui-red)',
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word'
}
const set_selectStyle = { color: 'var(--ui-text-primary)' }

// --- Pure helpers ---------------------------------------------------------------

// jsxs wrapper: set_h(type, props, ...children). Children are static, `key` is hoisted.
function set_h(type, props, ...kids) {
  const { key, ...p } = props || {}
  // Void elements (input, img, …) must not receive a children prop, not even [].
  return kids.length === 0 ? jsx(type, p, key) : jsxs(type, { ...p, children: kids }, key)
}

function set_clone(o) {
  return JSON.parse(JSON.stringify(o))
}

function set_same(a, b) {
  return JSON.stringify(a) === JSON.stringify(b)
}

function set_getIn(obj, path, fallback) {
  let cur = obj
  for (const k of path) {
    if (cur === null || cur === undefined || typeof cur !== 'object') {
      return fallback
    }
    cur = cur[k]
  }
  return cur === undefined ? fallback : cur
}

function set_setIn(obj, path, value) {
  if (!path.length) {
    return value
  }
  const [k, ...tail] = path
  const base = obj && typeof obj === 'object' ? obj : {}
  const copy = Array.isArray(base) ? base.slice() : { ...base }
  copy[k] = set_setIn(base[k], tail, value)
  return copy
}

function set_toggleIn(list, value) {
  return list.includes(value) ? list.filter(v => v !== value) : [...list, value]
}

// Number when the text is numeric, otherwise the raw text so the server's 422 names the problem.
function set_num(text) {
  const t = String(text).trim()
  if (t === '') {
    return null
  }
  const n = Number(t)
  return Number.isFinite(n) ? n : t
}

const set_fmtNum = v => (v === null || v === undefined ? '' : String(v))
const set_splitList = t =>
  String(t)
    .split(',')
    .map(s => s.trim())
    .filter(Boolean)
const set_fmtList = v => (Array.isArray(v) ? v.join(', ') : '')
const set_splitNums = t => set_splitList(t).map(s => set_num(s))
const set_splitLines = t =>
  String(t)
    .split('\n')
    .map(s => s.trim())
    .filter(Boolean)
const set_fmtLines = v => (Array.isArray(v) ? v.join('\n') : '')
const set_fmtRanges = v => (Array.isArray(v) ? v.map(p => (Array.isArray(p) ? p.join('-') : String(p))).join(', ') : '')
const set_parseRanges = t =>
  set_splitList(t).map(s => {
    const i = s.indexOf('-')
    return i < 0 ? [s, ''] : [s.slice(0, i).trim(), s.slice(i + 1).trim()]
  })

// FastAPI 422 detail arrays become "field.path: message" lines.
function set_errMsg(err) {
  const msg = String((err && err.message) || err)
  const m = /^(\d{3}):\s*([\s\S]*)$/.exec(msg)
  if (m) {
    try {
      const body = JSON.parse(m[2])
      const d = body && body.detail
      if (Array.isArray(d)) {
        return d
          .map(x => {
            const loc = Array.isArray(x.loc) ? x.loc.filter(p => p !== 'body').join('.') : ''
            return (loc ? loc + ': ' : '') + (x.msg || JSON.stringify(x))
          })
          .join('\n')
      }
      if (typeof d === 'string') {
        return d + restartHint(err)
      }
    } catch {
      // not JSON: fall through
    }
  }
  return errorText(err) + restartHint(err)
}

// Calls the backend and returns {ok, data} or {ok:false, error} (error text kept for inline display).
async function set_call(path, method, body) {
  try {
    const data = await rest(path, { method, body })
    refresh()
    return { ok: true, data }
  } catch (err) {
    return { ok: false, error: set_errMsg(err) }
  }
}

function set_nowSec() {
  return Math.floor(Date.now() / 1000)
}

function set_ago(ts) {
  if (!ts) {
    return 'never'
  }
  const s = Math.max(0, set_nowSec() - ts)
  return s < 60 ? s + 's ago' : fmtAge(s) + ' ago'
}

// segno SVGs carry fixed width/height; make them fill the QR box.
function set_fitSvg(svg) {
  return String(svg).replace(/<svg\b([^>]*)>/i, (_m, attrs) => {
    let a = attrs
    const w = /\swidth="([\d.]+)/.exec(a)
    const h = /\sheight="([\d.]+)/.exec(a)
    const hasViewBox = /\sviewBox=/i.test(a)
    a = a.replace(/\s(width|height)="[^"]*"/gi, '')
    if (!hasViewBox && w && h) {
      a += ' viewBox="0 0 ' + w[1] + ' ' + h[1] + '"'
    }
    return '<svg' + a + ' width="100%" height="100%">'
  })
}

function set_ruleIn(rule) {
  return {
    name: rule.name,
    enabled: !!rule.enabled,
    account_id: rule.account_id === undefined ? null : rule.account_id,
    event_types: rule.event_types,
    conditions: rule.conditions || {},
    action: rule.action,
    reply_mode: rule.reply_mode,
    stop_after_match: !!rule.stop_after_match
  }
}

function set_ruleToForm(rule) {
  const r = rule || {}
  const c = r.conditions || {}
  const a = r.action || { type: 'hermes' }
  const type = a.type || 'hermes'
  const triState = v => (v === null || v === undefined ? '' : v ? 'yes' : 'no')
  return {
    name: r.name || '',
    enabled: r.enabled !== false,
    account_id: r.account_id === null || r.account_id === undefined ? '' : String(r.account_id),
    event_types: r.event_types || ['message.in'],
    states: c.states || [],
    agent_active: triState(c.agent_active),
    business_hours: c.business_hours || '',
    text_contains: c.text_contains || [],
    text_regex: c.text_regex || '',
    tags_any: c.tags_any || [],
    first_message: triState(c.first_message),
    directions: c.directions || [],
    type,
    profile: a.profile || '',
    prompt: rule ? a.prompt || '' : SETTINGS_DEFAULT_PROMPT,
    timeout: String(a.timeout_s !== null && a.timeout_s !== undefined ? a.timeout_s : SETTINGS_TIMEOUTS[type] || ''),
    skills: a.skills || [],
    url: a.url || '',
    secret: a.secret || '',
    command: a.command || [],
    text: a.text || '',
    state: a.state || 'in_progress',
    tags: a.tags || [],
    reply_mode: r.reply_mode || 'draft',
    stop_after_match: !!r.stop_after_match
  }
}

// Returns {rule} or {error}.
function set_formToRule(f) {
  if (!f.name.trim()) {
    return { error: 'Name is required.' }
  }
  if (!f.event_types.length) {
    return { error: 'Select at least one event type.' }
  }
  const cond = {}
  if (f.states.length) {
    cond.states = f.states
  }
  if (f.agent_active) {
    cond.agent_active = f.agent_active === 'yes'
  }
  if (f.business_hours) {
    cond.business_hours = f.business_hours
  }
  if (f.text_contains.length) {
    cond.text_contains = f.text_contains
  }
  if (f.text_regex.trim()) {
    cond.text_regex = f.text_regex
  }
  if (f.tags_any.length) {
    cond.tags_any = f.tags_any
  }
  if (f.first_message) {
    cond.first_message = f.first_message === 'yes'
  }
  if (f.directions.length) {
    cond.directions = f.directions
  }
  const timeout = Number(f.timeout)
  const timeout_s = Number.isFinite(timeout) && timeout > 0 ? Math.round(timeout) : SETTINGS_TIMEOUTS[f.type]
  let action
  if (f.type === 'hermes') {
    if (!f.prompt.trim()) {
      return { error: 'The prompt template is required.' }
    }
    action = { type: 'hermes', profile: f.profile.trim() || null, prompt: f.prompt, timeout_s, skills: f.skills }
  } else if (f.type === 'webhook') {
    if (!f.url.trim()) {
      return { error: 'The webhook URL is required.' }
    }
    action = { type: 'webhook', url: f.url.trim(), secret: f.secret.trim() || null, timeout_s }
  } else if (f.type === 'script') {
    if (!f.command.length) {
      return { error: 'The script command is required (one argument per line).' }
    }
    action = { type: 'script', command: f.command, timeout_s }
  } else if (f.type === 'reply') {
    if (!f.text.trim()) {
      return { error: 'The reply text is required.' }
    }
    action = { type: 'reply', text: f.text }
  } else if (f.type === 'set_state') {
    action = { type: 'set_state', state: f.state }
  } else if (f.type === 'add_tags') {
    if (!f.tags.length) {
      return { error: 'Enter at least one tag.' }
    }
    action = { type: 'add_tags', tags: f.tags }
  } else {
    action = { type: f.type }
  }
  return {
    rule: {
      name: f.name.trim(),
      enabled: f.enabled,
      account_id: f.account_id === '' ? null : Number(f.account_id),
      event_types: f.event_types,
      conditions: cond,
      action,
      reply_mode: f.reply_mode,
      stop_after_match: f.stop_after_match
    }
  }
}

function set_ruleSummary(rule) {
  const a = rule.action || {}
  const found = SETTINGS_ACTIONS.find(x => x[0] === a.type)
  return found ? found[1] : a.type || 'Unknown'
}

function set_clip(text, n) {
  const s = String(text || '')
  return s.length > n ? s.slice(0, n) + '…' : s
}

// --- Form primitives ----------------------------------------------------------------

function SettingsBadge({ variant, children }) {
  return jsx(Badge, { variant, children })
}

function SettingsField({ label, hint, children }) {
  return set_h(
    'div',
    { className: 'flex flex-col gap-1 min-w-0' },
    label ? set_h('span', { className: 'text-xs font-medium', style: set_secondary }, label) : null,
    children,
    hint ? set_h('span', { className: 'text-xs', style: set_muted }, hint) : null
  )
}

function SettingsToggle({ checked, onChange, label, hint, disabled }) {
  return set_h(
    'button',
    {
      type: 'button',
      role: 'switch',
      'aria-checked': !!checked,
      disabled,
      onClick: () => onChange(!checked),
      className: 'flex items-start gap-3 text-left',
      style: {
        background: 'none',
        border: 'none',
        padding: 0,
        cursor: disabled ? 'default' : 'pointer',
        color: 'var(--ui-text-primary)',
        opacity: disabled ? 0.5 : 1
      }
    },
    set_h(
      'span',
      {
        className: 'shrink-0',
        style: {
          position: 'relative',
          display: 'inline-block',
          width: 32,
          height: 18,
          borderRadius: 9,
          marginTop: 1,
          background: checked ? 'var(--ui-accent)' : 'var(--ui-bg-quaternary)',
          boxShadow: 'inset 0 0 0 1px var(--ui-stroke-secondary)',
          transition: 'background 120ms'
        }
      },
      set_h('span', {
        style: {
          position: 'absolute',
          top: 2,
          left: checked ? 16 : 2,
          width: 14,
          height: 14,
          borderRadius: 7,
          background: 'var(--ui-base)',
          transition: 'left 120ms'
        }
      })
    ),
    set_h(
      'span',
      { className: 'flex flex-col gap-0.5 min-w-0' },
      set_h('span', { className: 'text-xs font-medium' }, label),
      hint ? set_h('span', { className: 'text-xs', style: set_muted }, hint) : null
    )
  )
}

function SettingsCheck({ checked, onChange, label, disabled }) {
  return set_h(
    'label',
    { className: 'inline-flex items-center gap-1.5 text-xs cursor-pointer' },
    set_h('input', {
      type: 'checkbox',
      checked: !!checked,
      disabled,
      onChange: e => onChange(e.target.checked),
      style: { accentColor: 'var(--ui-accent)' }
    }),
    set_h('span', { style: { color: 'var(--ui-text-primary)' } }, label)
  )
}

function SettingsSelect({ value, onChange, options, disabled }) {
  return set_h(
    'select',
    {
      value: value === null || value === undefined ? '' : value,
      disabled,
      onChange: e => onChange(e.target.value),
      className: 'desktop-input-chrome w-full min-w-0 rounded-[2.5px] border px-2.5 py-1.5 text-xs',
      style: set_selectStyle
    },
    ...options.map(o => set_h('option', { key: o[0], value: o[0] }, o[1]))
  )
}

function SettingsSegmented({ value, onChange, options, disabled }) {
  return set_h(
    'div',
    { className: 'flex flex-wrap gap-1' },
    ...options.map(o =>
      jsx(Button, {
        key: o[0],
        type: 'button',
        size: 'sm',
        disabled,
        variant: value === o[0] ? 'default' : 'outline',
        onClick: () => onChange(o[0]),
        children: o[1]
      })
    )
  )
}

// Text input that edits a parsed value (numbers, comma lists, ranges, lines) without
// clobbering what the user is typing: the text is only rewritten on external changes.
function SettingsParsed({ value, format, parse, onChange, multiline, placeholder, rows, disabled, type, min, max }) {
  const [text, setText] = useState(() => format(value))
  useEffect(() => {
    if (format(value) !== format(parse(text))) {
      setText(format(value))
    }
  }, [JSON.stringify(value)])
  const handle = e => {
    setText(e.target.value)
    onChange(parse(e.target.value))
  }
  if (multiline) {
    return jsx(Textarea, { value: text, onChange: handle, placeholder, rows: rows || 3, disabled })
  }
  return jsx(Input, { value: text, onChange: handle, placeholder, disabled, type: type || 'text', min, max })
}

function SettingsNumber({ value, onChange, min, max, placeholder, disabled }) {
  return jsx(SettingsParsed, {
    value,
    onChange,
    format: set_fmtNum,
    parse: t => set_num(t),
    type: 'number',
    min,
    max,
    placeholder,
    disabled
  })
}

function SettingsListInput({ value, onChange, placeholder, numbers }) {
  return jsx(SettingsParsed, {
    value,
    onChange,
    format: set_fmtList,
    parse: numbers ? set_splitNums : set_splitList,
    placeholder
  })
}

function SettingsCard({ title, desc, children }) {
  return set_h(
    'div',
    { className: 'flex flex-col gap-3', style: set_card },
    title || desc
      ? set_h(
          'div',
          { className: 'flex flex-col gap-0.5' },
          title ? set_h('div', { className: 'text-sm font-semibold' }, title) : null,
          desc ? set_h('div', { className: 'text-xs', style: set_muted }, desc) : null
        )
      : null,
    children
  )
}

function SettingsColorDot({ color, size }) {
  const s = size || 10
  return set_h('span', {
    className: 'inline-block shrink-0',
    style: { width: s, height: s, borderRadius: s, background: ACCOUNT_COLORS[color] || ACCOUNT_COLORS.gray }
  })
}

function SettingsColorPicker({ value, onChange }) {
  return set_h(
    'div',
    { className: 'flex flex-wrap gap-1.5' },
    ...Object.keys(ACCOUNT_COLORS).map(name =>
      set_h(
        'button',
        {
          key: name,
          type: 'button',
          title: name,
          'aria-label': name,
          onClick: () => onChange(name),
          className: 'inline-flex items-center justify-center',
          style: {
            width: 24,
            height: 24,
            borderRadius: 12,
            background: 'none',
            cursor: 'pointer',
            border: value === name ? '2px solid var(--ui-text-primary)' : '2px solid transparent'
          }
        },
        jsx(SettingsColorDot, { color: name, size: 14 })
      )
    )
  )
}

function SettingsConfirm({ message, confirmLabel, onConfirm, onCancel, busy, children }) {
  return set_h(
    'div',
    { className: 'flex flex-col gap-2', style: { ...set_subtle, borderColor: 'var(--ui-red)' } },
    set_h('div', { className: 'text-xs' }, message),
    children,
    set_h(
      'div',
      { className: 'flex gap-2' },
      jsx(Button, { size: 'sm', variant: 'destructive', loading: busy, onClick: onConfirm, children: confirmLabel }),
      jsx(Button, { size: 'sm', variant: 'outline', disabled: busy, onClick: onCancel, children: 'Cancel' })
    )
  )
}

// --- Section 1: numbers -------------------------------------------------------------------

function SettingsAccountForm({ initial, isNew, busy, error, onSubmit, onCancel }) {
  const [label, setLabel] = useState(initial.label || '')
  const [color, setColor] = useState(initial.color || 'blue')
  const [history, setHistory] = useState(initial.history_mode || 'recent')
  const [profile, setProfile] = useState(initial.hermes_profile || '')
  const hist = SETTINGS_HISTORY_MODES.find(h => h[0] === history)
  const submit = () => {
    const out = { label: label.trim(), color, history_mode: history }
    if (!isNew) {
      out.hermes_profile = profile.trim() || null
    }
    onSubmit(out)
  }
  return set_h(
    'div',
    { className: 'flex flex-col gap-3', style: set_subtle },
    jsx(SettingsField, {
      label: 'Label',
      children: jsx(Input, { value: label, onChange: e => setLabel(e.target.value), placeholder: 'e.g. Sales' })
    }),
    jsx(SettingsField, { label: 'Color', children: jsx(SettingsColorPicker, { value: color, onChange: setColor }) }),
    jsx(SettingsField, {
      label: 'History import',
      hint: (hist ? hist[2] + ' ' : '') + 'Imported conversations start closed and never trigger rules or automations.',
      children: jsx(SettingsSegmented, {
        value: history,
        onChange: setHistory,
        options: SETTINGS_HISTORY_MODES.map(h => [h[0], h[1]])
      })
    }),
    isNew
      ? null
      : jsx(SettingsField, {
          label: 'Hermes profile',
          hint: 'Profile used for this number. Leave empty for the default profile.',
          children: jsx(Input, { value: profile, onChange: e => setProfile(e.target.value), placeholder: 'default' })
        }),
    error ? set_h('div', { className: 'text-xs', style: set_errorBox }, error) : null,
    set_h(
      'div',
      { className: 'flex gap-2' },
      jsx(Button, {
        size: 'sm',
        loading: busy,
        disabled: !label.trim(),
        onClick: submit,
        children: isNew ? 'Add number and pair' : 'Save'
      }),
      jsx(Button, { size: 'sm', variant: 'outline', disabled: busy, onClick: onCancel, children: 'Cancel' })
    )
  )
}

function SettingsAccountRow({ account, onPair }) {
  const [editing, setEditing] = useState(false)
  const [confirm, setConfirm] = useState(null)
  const [deleteConv, setDeleteConv] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const isDemo = account.kind === 'demo'
  const st = account.status
  const state = st ? st.state : 'stopped'
  const badge = SETTINGS_STATE_BADGE[state] || [state, 'muted']
  const hbAge = st && st.heartbeat_at ? set_nowSec() - st.heartbeat_at : null
  const stale = hbAge === null || hbAge > 15
  const running = account.desired === 'running'
  const removing = account.desired === 'removed'
  const who = [account.phone ? '+' + String(account.phone).replace(/^\+/, '') : null, account.wa_name]
    .filter(Boolean)
    .join(' · ')

  const run = async fn => {
    setBusy(true)
    try {
      await fn()
    } finally {
      setBusy(false)
    }
  }
  const simple = verb => run(() => act('/accounts/' + account.id + '/' + verb, undefined))
  const saveEdit = async values => {
    setBusy(true)
    setError('')
    const res = await set_call('/accounts/' + account.id, 'PATCH', values)
    setBusy(false)
    if (res.ok) {
      setEditing(false)
    } else {
      setError(res.error)
    }
  }

  return set_h(
    'div',
    { className: 'flex flex-col gap-2', style: set_card },
    set_h(
      'div',
      { className: 'flex flex-wrap items-center gap-2' },
      jsx(SettingsColorDot, { color: account.color, size: 12 }),
      set_h('span', { className: 'text-sm font-medium' }, account.label),
      isDemo
        ? jsx(SettingsBadge, { variant: 'outline', children: 'Demo' })
        : removing
          ? jsx(SettingsBadge, { variant: 'muted', children: 'Removing…' })
          : jsx(SettingsBadge, { variant: badge[1], children: badge[0] }),
      !isDemo && !running && !removing && account.desired !== 'logged_out'
        ? jsx(SettingsBadge, { variant: 'outline', children: 'Stopped by you' })
        : null,
      who ? set_h('span', { className: 'text-xs', style: set_secondary }, who) : null,
      isDemo
        ? null
        : set_h(
            'span',
            { className: 'text-xs ml-auto', style: stale ? { color: 'var(--ui-orange)' } : set_muted },
            stale
              ? st && st.heartbeat_at
                ? 'No heartbeat for ' + fmtAge(hbAge) + ' · service not running'
                : 'No heartbeat · service not running'
              : 'Heartbeat ' + set_ago(st.heartbeat_at)
          )
    ),
    !isDemo && st && st.error ? set_h('div', { className: 'text-xs', style: set_errorBox }, st.error) : null,
    editing
      ? jsx(SettingsAccountForm, {
          initial: account,
          isNew: false,
          busy,
          error,
          onSubmit: saveEdit,
          onCancel: () => {
            setEditing(false)
            setError('')
          }
        })
      : null,
    confirm === 'logout'
      ? jsx(SettingsConfirm, {
          message:
            'Log out ' +
            account.label +
            '? The linked session is deleted from this computer. Pairing again requires scanning a new QR code.',
          confirmLabel: 'Log out',
          busy,
          onCancel: () => setConfirm(null),
          onConfirm: () =>
            run(async () => {
              await act('/accounts/' + account.id + '/logout', undefined)
              setConfirm(null)
            })
        })
      : null,
    confirm === 'remove'
      ? jsx(SettingsConfirm, {
          message: 'Remove ' + account.label + '? The number is unlinked and removed from the plugin.',
          confirmLabel: 'Remove',
          busy,
          onCancel: () => setConfirm(null),
          onConfirm: () =>
            run(async () => {
              await act(
                '/accounts/' + account.id + '?delete_conversations=' + (deleteConv ? 'true' : 'false'),
                undefined,
                'DELETE'
              )
              setConfirm(null)
            }),
          children: jsx(SettingsCheck, {
            checked: deleteConv,
            onChange: setDeleteConv,
            label: 'Also delete its conversations and messages'
          })
        })
      : null,
    removing || editing || confirm
      ? null
      : set_h(
          'div',
          { className: 'flex flex-wrap gap-1.5' },
          isDemo
            ? null
            : [
                !running
                  ? jsx(Button, {
                      key: 'start',
                      size: 'xs',
                      variant: 'secondary',
                      disabled: busy,
                      onClick: () => simple('start'),
                      children: 'Start'
                    })
                  : null,
                running
                  ? jsx(Button, {
                      key: 'stop',
                      size: 'xs',
                      variant: 'secondary',
                      disabled: busy,
                      onClick: () => simple('stop'),
                      children: 'Stop'
                    })
                  : null,
                running
                  ? jsx(Button, {
                      key: 'restart',
                      size: 'xs',
                      variant: 'secondary',
                      disabled: busy,
                      onClick: () => simple('restart'),
                      children: 'Restart'
                    })
                  : null,
                running && !account.paired
                  ? jsx(Button, {
                      key: 'pair',
                      size: 'xs',
                      variant: 'secondary',
                      onClick: () => onPair(account.id),
                      children: 'Show QR'
                    })
                  : null,
                jsx(Button, {
                  key: 'edit',
                  size: 'xs',
                  variant: 'outline',
                  onClick: () => setEditing(true),
                  children: 'Edit'
                }),
                jsx(Button, {
                  key: 'logout',
                  size: 'xs',
                  variant: 'outline',
                  onClick: () => setConfirm('logout'),
                  children: 'Log out'
                })
              ],
          jsx(Button, { size: 'xs', variant: 'outline', onClick: () => setConfirm('remove'), children: 'Remove' })
        )
  )
}

function SettingsPairing({ accountId, onClose }) {
  const q = useQuery({
    queryKey: [ID, 'pairing', accountId],
    queryFn: () => rest('/accounts'),
    refetchInterval: 2000
  })
  const [auto, setAuto] = useState(false)
  const [manual, setManual] = useState(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    // Dark themes render currentColor light; flip the QR so it is dark-on-light for scanners.
    const probe = document.createElement('span')
    probe.style.color = 'var(--ui-text-primary)'
    document.body.appendChild(probe)
    const m = /rgba?\((\d+)[ ,]+(\d+)[ ,]+(\d+)/.exec(getComputedStyle(probe).color)
    document.body.removeChild(probe)
    if (m) {
      setAuto(0.299 * Number(m[1]) + 0.587 * Number(m[2]) + 0.114 * Number(m[3]) > 140)
    }
  }, [])
  const invert = manual === null ? auto : manual
  const account = ((q.data && q.data.accounts) || []).find(a => a.id === accountId)
  const st = account && account.status
  const state = st ? st.state : null

  if (q.error && !q.data) {
    return jsx(ErrorState, { title: 'Backend unreachable', description: errorText(q.error) })
  }
  if (!account) {
    return set_h(
      'div',
      { className: 'flex flex-col gap-3' },
      q.data
        ? set_h('div', { className: 'text-xs', style: set_muted }, 'This number no longer exists.')
        : jsx(Skeleton, { className: 'h-40 w-full' }),
      jsx(Button, { size: 'sm', variant: 'outline', onClick: onClose, children: 'Back to numbers' })
    )
  }

  const startPairing = async () => {
    setBusy(true)
    await act('/accounts/' + account.id + '/' + (account.desired === 'running' ? 'restart' : 'start'), undefined)
    setBusy(false)
  }

  let body
  if (state === 'connected') {
    body = set_h(
      'div',
      { className: 'flex flex-col items-center gap-2 text-center', style: { ...set_card, padding: 24 } },
      jsx(SettingsBadge, { variant: 'success', children: 'Connected' }),
      set_h('div', { className: 'text-base font-semibold' }, account.label + ' is linked'),
      set_h(
        'div',
        { className: 'text-sm', style: set_secondary },
        [account.phone ? '+' + String(account.phone).replace(/^\+/, '') : null, account.wa_name]
          .filter(Boolean)
          .join(' · ') || 'WhatsApp account linked'
      ),
      jsx(Button, { size: 'sm', onClick: onClose, children: 'Done' })
    )
  } else {
    const showQr = st && st.qr_svg && (state === 'qr' || state === 'pairing')
    const waiting = running => (running ? 'Waiting for the QR code…' : 'Pairing is not running.')
    body = set_h(
      'div',
      { className: 'flex flex-wrap gap-6' },
      set_h(
        'div',
        { className: 'flex flex-col items-center gap-2' },
        showQr
          ? set_h('div', {
              role: 'img',
              'aria-label': 'WhatsApp pairing QR code',
              dangerouslySetInnerHTML: { __html: set_fitSvg(st.qr_svg) },
              style: {
                width: 248,
                height: 248,
                padding: 12,
                boxSizing: 'border-box',
                borderRadius: 8,
                color: 'var(--ui-text-primary)',
                background: 'var(--ui-bg-elevated)',
                border: '1px solid var(--ui-stroke-secondary)',
                filter: invert ? 'invert(1)' : 'none'
              }
            })
          : set_h(
              'div',
              {
                className: 'flex flex-col items-center justify-center gap-2 text-xs',
                style: {
                  width: 248,
                  height: 248,
                  borderRadius: 8,
                  border: '1px dashed var(--ui-stroke-secondary)',
                  ...set_muted
                }
              },
              waiting(account.desired === 'running'),
              state
                ? jsx(SettingsBadge, {
                    variant: (SETTINGS_STATE_BADGE[state] || [0, 'muted'])[1],
                    children: (SETTINGS_STATE_BADGE[state] || [state])[0]
                  })
                : null
            ),
        showQr
          ? jsx(Button, {
              size: 'xs',
              variant: 'text',
              onClick: () => setManual(!invert),
              children: invert ? 'Use original QR colors' : "Can't scan? Invert QR colors"
            })
          : null,
        st && st.qr_at && showQr
          ? set_h('span', { className: 'text-xs', style: set_muted }, 'QR updated ' + set_ago(st.qr_at))
          : null
      ),
      set_h(
        'div',
        { className: 'flex flex-col gap-3 min-w-0', style: { flex: '1 1 240px' } },
        set_h('div', { className: 'text-sm font-semibold' }, 'Link ' + account.label + ' to WhatsApp'),
        set_h(
          'ol',
          {
            className: 'flex flex-col gap-1 text-xs',
            style: { ...set_secondary, listStyle: 'decimal', paddingLeft: 18 }
          },
          set_h('li', {}, 'Open WhatsApp on the phone you want to link.'),
          set_h('li', {}, 'Go to Settings → Linked devices → Link a device.'),
          set_h('li', {}, 'Point the phone at the QR code on this screen.')
        ),
        set_h(
          'div',
          { className: 'text-xs', style: set_muted },
          'The QR code refreshes automatically; this view updates every 2 seconds.'
        ),
        st && st.error ? set_h('div', { className: 'text-xs', style: set_errorBox }, st.error) : null,
        set_h(
          'div',
          { className: 'flex flex-wrap gap-2' },
          jsx(Button, {
            size: 'sm',
            variant: 'secondary',
            loading: busy,
            onClick: startPairing,
            children:
              state === 'logged_out' || state === 'stopped' || account.desired !== 'running'
                ? 'Start pairing'
                : 'Restart pairing'
          }),
          jsx(Button, { size: 'sm', variant: 'outline', onClick: onClose, children: 'Close' })
        )
      )
    )
  }
  return set_h(
    'div',
    { className: 'flex flex-col gap-3' },
    set_h(
      'div',
      { className: 'flex items-center gap-2' },
      jsx(SettingsColorDot, { color: account.color, size: 12 }),
      set_h('span', { className: 'text-sm font-semibold' }, 'Pair ' + account.label)
    ),
    body
  )
}

function SettingsKeyValue({ label, value, warn }) {
  return set_h(
    'div',
    { className: 'flex items-baseline gap-2 text-xs' },
    set_h('span', { style: { ...set_muted, width: 96, flex: '0 0 auto' } }, label),
    set_h('span', { className: 'min-w-0', style: warn ? { color: 'var(--ui-orange)' } : set_secondary }, value)
  )
}

function SettingsServiceCard() {
  const q = useApi('service', '/service')
  const [busy, run] = useServiceAction()
  const outdated = useBackendOutdated()
  const restart = useValue($restart)
  const [confirm, setConfirm] = useState(false)
  const s = q.data
  if (!s) {
    return q.error
      ? jsx(SettingsCard, {
          title: 'Service',
          children: set_h('div', { className: 'text-xs', style: set_errorBox }, errorText(q.error))
        })
      : jsx(Skeleton, { className: 'h-24 w-full' })
  }
  const installed = s.installed !== false
  const restarting = restart === 'restarting'
  const locked = busy !== null || restarting || outdated
  const lockTitle = outdated ? RESTART_TITLE : undefined
  const badge = s.running
    ? ['success', 'Running']
    : installed
      ? ['destructive', 'Not running']
      : ['muted', 'Not installed']
  const nodeKnown = Object.prototype.hasOwnProperty.call(s, 'node')
  return jsx(SettingsCard, {
    title: 'Service',
    desc: 'The background process that keeps your numbers connected and delivers messages. It starts at login and restarts if it stops.',
    children: set_h(
      'div',
      { className: 'flex flex-col gap-2' },
      set_h(
        'div',
        { className: 'flex items-center gap-2' },
        jsx(SettingsBadge, { variant: badge[0], children: badge[1] })
      ),
      jsx(SettingsKeyValue, { label: 'Installed', value: installed ? 'Yes' : 'No' }),
      jsx(SettingsKeyValue, { label: 'Running', value: s.running ? 'Yes' : 'No' }),
      s.running ? jsx(SettingsKeyValue, { label: 'PID', value: String(s.pid) }) : null,
      s.version ? jsx(SettingsKeyValue, { label: 'Version', value: String(s.version) }) : null,
      jsx(SettingsKeyValue, {
        label: 'Node.js',
        value: s.node
          ? s.node
          : nodeKnown
            ? 'Not found. Install Node.js, then install the service.'
            : 'unknown (restart Hermes)',
        warn: !s.node
      }),
      s.heartbeat_at ? jsx(SettingsKeyValue, { label: 'Heartbeat', value: set_ago(s.heartbeat_at) }) : null,
      restarting
        ? set_h(
            'div',
            { className: 'text-xs', style: set_secondary },
            'Restarting service… waiting for it to report in (up to 30 s).'
          )
        : null,
      restart === 'failed' && !s.running
        ? set_h(
            'div',
            { className: 'text-xs', style: set_errorBox },
            'The service did not start within 30 s. Check the log: ',
            set_h('code', { style: CODE }, SERVICE_LOG_HINT)
          )
        : null,
      installed && !s.running && !restarting && restart !== 'failed'
        ? set_h(
            'div',
            { className: 'text-xs', style: set_muted },
            'Not running. Try Reinstall; details are written to ',
            set_h('code', { style: CODE }, SERVICE_LOG_HINT)
          )
        : null,
      outdated ? set_h('div', { className: 'text-xs', style: { color: 'var(--ui-orange)' } }, RESTART_TITLE) : null,
      confirm
        ? jsx(SettingsConfirm, {
            message:
              'Uninstall the service? Numbers stay linked but stop receiving and sending until it is installed again.',
            confirmLabel: 'Uninstall service',
            busy: busy === 'uninstall',
            onCancel: () => setConfirm(false),
            onConfirm: async () => {
              await run('uninstall', '/service/uninstall', 'WhatsApp service uninstalled')
              setConfirm(false)
            }
          })
        : set_h(
            'div',
            { className: 'flex flex-wrap gap-2' },
            jsx(Button, {
              size: 'sm',
              variant: installed ? 'secondary' : 'default',
              loading: busy === 'install' || restarting,
              disabled: locked,
              title: lockTitle,
              onClick: () => run('install', '/service/install', 'WhatsApp service installed'),
              children: installed ? 'Reinstall' : 'Install service'
            }),
            installed
              ? jsx(Button, {
                  size: 'sm',
                  variant: 'outline',
                  disabled: locked,
                  title: lockTitle,
                  onClick: () => setConfirm(true),
                  children: 'Uninstall service'
                })
              : null
          )
    )
  })
}

function SettingsSkillCard() {
  const q = useApi('service', '/service')
  const [busy, run] = useServiceAction()
  const outdated = useBackendOutdated()
  const s = q.data
  if (!s) {
    return null
  }
  const installed = Boolean(s.skill_installed)
  const lockTitle = outdated ? RESTART_TITLE : undefined
  return jsx(SettingsCard, {
    title: 'Hermes skill',
    desc: 'Lets Hermes agents list, read and answer your WhatsApp conversations. Installed into the Hermes skills folder.',
    children: set_h(
      'div',
      { className: 'flex flex-col gap-2' },
      set_h(
        'div',
        { className: 'flex items-center gap-2' },
        jsx(SettingsBadge, {
          variant: installed ? 'success' : 'muted',
          children: installed ? 'Installed' : 'Not installed'
        })
      ),
      outdated ? set_h('div', { className: 'text-xs', style: { color: 'var(--ui-orange)' } }, RESTART_TITLE) : null,
      set_h(
        'div',
        { className: 'flex flex-wrap gap-2' },
        jsx(Button, {
          size: 'sm',
          variant: installed ? 'secondary' : 'default',
          loading: busy === 'skill-install',
          disabled: busy !== null || outdated,
          title: lockTitle,
          onClick: () => run('skill-install', '/skill/install', 'Hermes skill installed'),
          children: installed ? 'Reinstall skill' : 'Install skill'
        }),
        installed
          ? jsx(Button, {
              size: 'sm',
              variant: 'outline',
              loading: busy === 'skill-remove',
              disabled: busy !== null || outdated,
              title: lockTitle,
              onClick: () => run('skill-remove', '/skill/uninstall', 'Hermes skill removed'),
              children: 'Remove skill'
            })
          : null
      )
    )
  })
}

function SettingsAccounts() {
  const q = useApi('accounts', '/accounts')
  const [adding, setAdding] = useState(false)
  const [pairId, setPairId] = useState(null)
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState('')
  const accounts = (q.data && q.data.accounts) || []

  const create = async values => {
    setCreating(true)
    setCreateError('')
    const res = await set_call('/accounts', 'POST', values)
    setCreating(false)
    if (res.ok) {
      setAdding(false)
      if (res.data && res.data.id !== undefined) {
        setPairId(res.data.id)
      }
    } else {
      setCreateError(res.error)
    }
  }

  if (pairId !== null) {
    return jsx(SettingsPairing, { accountId: pairId, onClose: () => setPairId(null) })
  }
  if (q.error && !q.data) {
    return jsx(ErrorState, { title: 'Backend unreachable', description: errorText(q.error) })
  }
  return set_h(
    'div',
    { className: 'flex flex-col gap-3' },
    set_h(
      'div',
      { className: 'flex items-center justify-between gap-2' },
      set_h('div', { className: 'text-sm font-semibold' }, 'WhatsApp numbers'),
      adding ? null : jsx(Button, { size: 'sm', onClick: () => setAdding(true), children: 'Add number' })
    ),
    adding
      ? jsx(SettingsAccountForm, {
          initial: { label: '', color: 'blue', history_mode: 'recent' },
          isNew: true,
          busy: creating,
          error: createError,
          onSubmit: create,
          onCancel: () => {
            setAdding(false)
            setCreateError('')
          }
        })
      : null,
    !q.data
      ? jsx(Skeleton, { className: 'h-20 w-full' })
      : accounts.length === 0
        ? set_h(
            'div',
            { className: 'text-xs', style: set_muted },
            'No numbers yet. Add a number to link a WhatsApp account by scanning a QR code.'
          )
        : accounts.map(a => jsx(SettingsAccountRow, { key: a.id, account: a, onPair: setPairId })),
    jsx(SettingsServiceCard, {}),
    jsx(SettingsSkillCard, {})
  )
}

// --- Sections 2-5: settings forms -------------------------------------------------------------

function SettingsRulesForm({ draft, set }) {
  const r = (k, fb) => set_getIn(draft, ['rules', k], fb)
  const p = k => v => set(['rules', k], v)
  return set_h(
    'div',
    { className: 'flex flex-col gap-3' },
    jsx(SettingsCard, {
      title: 'Inbound messages',
      desc: 'How a conversation moves when the contact writes.',
      children: set_h(
        'div',
        { className: 'flex flex-col gap-3' },
        jsx(SettingsField, {
          label: 'Contact writes to a closed conversation',
          hint: 'Reopen as New, go back to the state before it was closed, or leave it closed (the message is still stored).',
          children: jsx(SettingsSelect, {
            value: r('inbound_on_closed'),
            onChange: p('inbound_on_closed'),
            options: [
              ['reopen_new', 'Reopen as New'],
              ['reopen_previous', 'Return to the previous state'],
              ['keep', 'Keep closed']
            ]
          })
        }),
        jsx(SettingsField, {
          label: 'Contact replies while the conversation is Waiting',
          hint: 'Waiting means you are waiting for the contact; their reply usually puts it back in progress.',
          children: jsx(SettingsSelect, {
            value: r('inbound_on_waiting'),
            onChange: p('inbound_on_waiting'),
            options: [
              ['in_progress', 'Move to In progress'],
              ['keep', 'Keep Waiting']
            ]
          })
        }),
        jsx(SettingsField, {
          label: 'Contact writes to a muted conversation',
          hint: 'Only real messages count; reactions and poll votes never wake a muted conversation.',
          children: jsx(SettingsSelect, {
            value: r('inbound_on_muted'),
            onChange: p('inbound_on_muted'),
            options: [
              ['reopen_new', 'Reopen as New'],
              ['keep', 'Stay muted']
            ]
          })
        })
      )
    }),
    jsx(SettingsCard, {
      title: 'Outbound messages',
      desc: 'What happens when you or an agent reply.',
      children: set_h(
        'div',
        { className: 'flex flex-col gap-3' },
        jsx(SettingsField, {
          label: 'Reply to a New or In progress conversation',
          hint: 'Applies to replies from the board, an agent, the CLI, or typed on the linked phone.',
          children: jsx(SettingsSelect, {
            value: r('outbound_on_active'),
            onChange: p('outbound_on_active'),
            options: [
              ['waiting', 'Move to Waiting'],
              ['keep', 'Keep the current state']
            ]
          })
        }),
        jsx(SettingsField, {
          label: 'Auto-reply window (seconds, 0–300)',
          hint: 'A message typed on the linked phone within this many seconds after the contact’s last message is treated as a WhatsApp Business away/greeting auto-reply: it changes neither the state nor unread count. 0 disables.',
          children: jsx(SettingsNumber, {
            value: r('auto_reply_window_seconds'),
            onChange: p('auto_reply_window_seconds'),
            min: 0,
            max: 300
          })
        })
      )
    }),
    jsx(SettingsCard, {
      title: 'Timers',
      children: jsx(SettingsField, {
        label: 'Auto-close Waiting conversations after (days, 1–365)',
        hint: 'Waiting conversations with no message for this long are closed automatically. Leave empty to never auto-close.',
        children: jsx(SettingsNumber, {
          value: r('auto_close_waiting_days', null),
          onChange: p('auto_close_waiting_days'),
          min: 1,
          max: 365,
          placeholder: 'Never'
        })
      })
    })
  )
}

function SettingsNotificationsForm({ draft, set }) {
  const n = k => set_getIn(draft, ['notifications', k], false)
  const quiet = set_getIn(draft, ['notifications', 'quiet_hours'], null)
  return set_h(
    'div',
    { className: 'flex flex-col gap-3' },
    jsx(SettingsCard, {
      title: 'Desktop notifications',
      children: set_h(
        'div',
        { className: 'flex flex-col gap-3' },
        jsx(SettingsToggle, {
          checked: n('new_conversation'),
          onChange: v => set(['notifications', 'new_conversation'], v),
          label: 'New conversation',
          hint: 'Notify when a contact writes for the first time.'
        }),
        jsx(SettingsToggle, {
          checked: n('every_inbound'),
          onChange: v => set(['notifications', 'every_inbound'], v),
          label: 'Every inbound message',
          hint: 'Notify for every message received, not only new conversations.'
        }),
        jsx(SettingsToggle, {
          checked: n('escalation'),
          onChange: v => set(['notifications', 'escalation'], v),
          label: 'Escalations',
          hint: 'Notify when a conversation is escalated.'
        })
      )
    }),
    jsx(SettingsCard, {
      title: 'Quiet hours',
      desc: 'No notifications are raised during this window (in the timezone from the Hours section).',
      children: set_h(
        'div',
        { className: 'flex flex-col gap-3' },
        jsx(SettingsToggle, {
          checked: !!quiet,
          onChange: v => set(['notifications', 'quiet_hours'], v ? { start: '22:00', end: '08:00' } : null),
          label: 'Enable quiet hours'
        }),
        quiet
          ? set_h(
              'div',
              { className: 'flex flex-wrap gap-3' },
              jsx(SettingsField, {
                label: 'From',
                children: jsx(Input, {
                  type: 'time',
                  value: quiet.start || '',
                  onChange: e => set(['notifications', 'quiet_hours', 'start'], e.target.value)
                })
              }),
              jsx(SettingsField, {
                label: 'Until',
                children: jsx(Input, {
                  type: 'time',
                  value: quiet.end || '',
                  onChange: e => set(['notifications', 'quiet_hours', 'end'], e.target.value)
                })
              })
            )
          : null
      )
    })
  )
}

function SettingsPrivacyForm({ draft, set }) {
  return jsx(SettingsCard, {
    title: 'Read receipts',
    children: jsx(SettingsToggle, {
      checked: set_getIn(draft, ['privacy', 'send_read_receipts'], true),
      onChange: v => set(['privacy', 'send_read_receipts'], v),
      label: 'Send read receipts',
      hint: 'When you open a conversation, the contact sees blue ticks on their messages, like in WhatsApp. Automations and agents never send read receipts.'
    })
  })
}

function SettingsBoardForm({ draft, set }) {
  const b = (k, fb) => set_getIn(draft, ['board', k], fb)
  return jsx(SettingsCard, {
    title: 'Board',
    children: set_h(
      'div',
      { className: 'flex flex-col gap-3' },
      jsx(SettingsField, {
        label: 'Urgency threshold (hours)',
        hint: 'New or Waiting conversations older than this are marked as medium urgency.',
        children: jsx(SettingsNumber, {
          value: b('urgency_hours'),
          onChange: v => set(['board', 'urgency_hours'], v),
          min: 1
        })
      }),
      jsx(SettingsField, {
        label: 'Mute presets (hours, comma separated)',
        hint: 'Quick snooze durations offered on a conversation, e.g. 1, 8, 24, 168.',
        children: jsx(SettingsListInput, {
          value: b('mute_presets_hours', []),
          onChange: v => set(['board', 'mute_presets_hours'], v),
          numbers: true,
          placeholder: '1, 8, 24, 168'
        })
      }),
      jsx(SettingsField, {
        label: 'Drop mute (hours)',
        hint: 'Duration used when a conversation is dropped onto the Muted column.',
        children: jsx(SettingsNumber, {
          value: b('drop_mute_hours'),
          onChange: v => set(['board', 'drop_mute_hours'], v),
          min: 1
        })
      }),
      jsx(SettingsField, {
        label: 'Closed conversations limit',
        hint: 'Maximum number of closed conversations shown when closed ones are included.',
        children: jsx(SettingsNumber, {
          value: b('closed_limit'),
          onChange: v => set(['board', 'closed_limit'], v),
          min: 1
        })
      })
    )
  })
}

function SettingsHoursForm({ draft, set }) {
  return set_h(
    'div',
    { className: 'flex flex-col gap-3' },
    jsx(SettingsCard, {
      title: 'Timezone',
      children: jsx(SettingsField, {
        label: 'IANA timezone',
        hint: 'Used for business hours, quiet hours and automation conditions, e.g. Europe/Rome.',
        children: jsx(Input, {
          value: set_getIn(draft, ['hours', 'timezone'], ''),
          onChange: e => set(['hours', 'timezone'], e.target.value),
          placeholder: 'Europe/Rome'
        })
      })
    }),
    jsx(SettingsCard, {
      title: 'Business hours',
      desc: 'Ranges as HH:MM-HH:MM, separated by commas (e.g. 09:00-13:00, 14:00-18:00). Leave empty for a closed day.',
      children: set_h(
        'div',
        { className: 'flex flex-col gap-2' },
        ...SETTINGS_WEEKDAYS.map(([key, label]) =>
          set_h(
            'div',
            { key, className: 'flex items-center gap-3' },
            set_h('span', { className: 'text-xs shrink-0', style: { ...set_secondary, width: 80 } }, label),
            set_h(
              'div',
              { className: 'flex-1 min-w-0' },
              jsx(SettingsParsed, {
                value: set_getIn(draft, ['hours', 'business', key], []),
                onChange: v => set(['hours', 'business', key], v),
                format: set_fmtRanges,
                parse: set_parseRanges,
                placeholder: 'Closed'
              })
            )
          )
        )
      )
    })
  )
}

// --- Section 6: automations ------------------------------------------------------------------------

function SettingsRuleTest({ rule, onClose }) {
  const convQ = useApi('conversations-pick', '/conversations?limit=20')
  const [convId, setConvId] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')
  const convs = (convQ.data && convQ.data.conversations) || []
  const run = async () => {
    const id = Number(convId)
    if (!convId || !Number.isInteger(id) || id <= 0) {
      setError('Enter a valid conversation id.')
      return
    }
    setBusy(true)
    setError('')
    setResult(null)
    try {
      setResult(await rest('/automations/' + rule.id + '/test', { method: 'POST', body: { conversation_id: id } }))
    } catch (err) {
      setError(set_errMsg(err))
    }
    setBusy(false)
  }
  return set_h(
    'div',
    { className: 'flex flex-col gap-3', style: set_subtle },
    set_h('div', { className: 'text-sm font-semibold' }, 'Test “' + rule.name + '” on a conversation'),
    set_h(
      'div',
      { className: 'text-xs', style: set_muted },
      'Dry run: shows whether the rule matches and the rendered prompt. Nothing is executed or sent.'
    ),
    set_h(
      'div',
      { className: 'flex flex-wrap items-end gap-3' },
      set_h(
        'div',
        { style: { flex: '1 1 220px' } },
        jsx(SettingsField, {
          label: 'Recent conversation',
          children: jsx(SettingsSelect, {
            value: convs.some(c => String(c.id) === convId) ? convId : '',
            onChange: setConvId,
            options: [
              ['', convQ.data ? 'Pick a conversation…' : 'Loading…'],
              ...convs.map(c => [
                String(c.id),
                '#' +
                  c.id +
                  ' · ' +
                  (c.contact_name || c.phone || c.chat_jid) +
                  ' · ' +
                  (c.account_label || '') +
                  ' · ' +
                  (STATE_LABELS[c.state] || c.state)
              ])
            ]
          })
        })
      ),
      set_h(
        'div',
        { style: { width: 140 } },
        jsx(SettingsField, {
          label: 'or conversation id',
          children: jsx(Input, {
            value: convId,
            onChange: e => setConvId(e.target.value),
            placeholder: '123',
            type: 'number',
            min: 1
          })
        })
      ),
      jsx(Button, { size: 'sm', loading: busy, onClick: run, children: 'Run test' }),
      jsx(Button, { size: 'sm', variant: 'outline', onClick: onClose, children: 'Close' })
    ),
    error ? set_h('div', { className: 'text-xs', style: set_errorBox }, error) : null,
    result
      ? set_h(
          'div',
          { className: 'flex flex-col gap-2' },
          set_h(
            'div',
            { className: 'flex items-center gap-2 text-xs' },
            jsx(SettingsBadge, {
              variant: result.matches ? 'success' : 'destructive',
              children: result.matches ? 'Matches' : 'Does not match'
            })
          ),
          result.reasons && result.reasons.length
            ? set_h(
                'ul',
                {
                  className: 'flex flex-col gap-0.5 text-xs',
                  style: { ...set_secondary, listStyle: 'disc', paddingLeft: 18 }
                },
                ...result.reasons.map((reason, i) => set_h('li', { key: i }, reason))
              )
            : null,
          result.rendered_prompt
            ? set_h(
                'div',
                { className: 'flex flex-col gap-1' },
                set_h('span', { className: 'text-xs font-medium', style: set_secondary }, 'Rendered prompt'),
                set_h(
                  'pre',
                  {
                    className: 'text-xs',
                    style: {
                      ...set_card,
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-word',
                      maxHeight: 280,
                      overflow: 'auto',
                      margin: 0
                    }
                  },
                  result.rendered_prompt
                )
              )
            : null
        )
      : null
  )
}

function SettingsRuleForm({ rule, accounts, onDone, onCancel }) {
  const [f, setF] = useState(() => set_ruleToForm(rule))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const up = (k, v) => setF(prev => ({ ...prev, [k]: v }))
  const isReply = SETTINGS_REPLY_ACTIONS.includes(f.type)
  const modeInfo = SETTINGS_REPLY_MODES.find(m => m[0] === f.reply_mode)
  const triOptions = labels => [
    ['', 'Any'],
    ['yes', labels[0]],
    ['no', labels[1]]
  ]
  const appendVar = (key, v) => up(key, f[key] + (f[key] && !/\s$/.test(f[key]) ? ' ' : '') + v)

  const varHelp = key =>
    set_h(
      'div',
      { className: 'flex flex-col gap-1' },
      set_h('span', { className: 'text-xs', style: set_muted }, 'Variables (click to insert):'),
      set_h(
        'div',
        { className: 'flex flex-wrap gap-1' },
        ...SETTINGS_TEMPLATE_VARS.map(([v, desc]) =>
          set_h(
            'button',
            {
              key: v,
              type: 'button',
              title: desc,
              onClick: () => appendVar(key, v),
              className: 'text-xs',
              style: {
                cursor: 'pointer',
                padding: '1px 6px',
                borderRadius: 3,
                border: '1px solid var(--ui-stroke-secondary)',
                background: 'var(--ui-bg-quaternary)',
                color: 'var(--ui-text-primary)',
                fontFamily: 'monospace'
              }
            },
            v
          )
        )
      ),
      set_h(
        'ul',
        { className: 'flex flex-col text-xs', style: { ...set_muted, listStyle: 'none', paddingLeft: 0 } },
        ...SETTINGS_TEMPLATE_VARS.map(([v, desc]) =>
          set_h('li', { key: v }, set_h('code', { style: set_secondary }, v), ' — ' + desc)
        )
      )
    )

  const save = async () => {
    const built = set_formToRule(f)
    if (built.error) {
      setError(built.error)
      return
    }
    setBusy(true)
    setError('')
    const res = rule
      ? await set_call('/automations/' + rule.id, 'PUT', built.rule)
      : await set_call('/automations', 'POST', built.rule)
    setBusy(false)
    if (res.ok) {
      onDone()
    } else {
      setError(res.error)
    }
  }

  const timeoutField = jsx(SettingsField, {
    label: 'Timeout (seconds)',
    children: jsx(Input, { type: 'number', min: 1, value: f.timeout, onChange: e => up('timeout', e.target.value) })
  })

  let actionFields = null
  if (f.type === 'hermes') {
    actionFields = set_h(
      'div',
      { className: 'flex flex-col gap-3' },
      jsx(SettingsField, {
        label: 'Hermes profile',
        hint: 'Runs `hermes -p <profile> chat`. Leave empty for the default profile.',
        children: jsx(Input, { value: f.profile, onChange: e => up('profile', e.target.value), placeholder: 'default' })
      }),
      jsx(SettingsField, {
        label: 'Prompt template',
        hint: 'The agent’s reply text (stdout) becomes the conversation reply, handled per the reply mode below.',
        children: jsx(Textarea, { value: f.prompt, rows: 9, onChange: e => up('prompt', e.target.value) })
      }),
      varHelp('prompt'),
      set_h(
        'div',
        { className: 'flex flex-wrap gap-3' },
        set_h('div', { style: { width: 160 } }, timeoutField),
        set_h(
          'div',
          { style: { flex: '1 1 220px' } },
          jsx(SettingsField, {
            label: 'Skills (comma separated)',
            children: jsx(SettingsListInput, {
              value: f.skills,
              onChange: v => up('skills', v),
              placeholder: 'whatsapp-chat'
            })
          })
        )
      )
    )
  } else if (f.type === 'webhook') {
    actionFields = set_h(
      'div',
      { className: 'flex flex-col gap-3' },
      jsx(SettingsField, {
        label: 'URL',
        hint: 'Receives a JSON POST {event, conversation, messages}. A JSON response may contain {reply, state, tags, escalate}.',
        children: jsx(Input, {
          value: f.url,
          onChange: e => up('url', e.target.value),
          placeholder: 'https://example.com/hook'
        })
      }),
      jsx(SettingsField, {
        label: 'Secret',
        hint: 'Optional. Used to sign the body in the X-WA-Signature header (HMAC SHA-256).',
        children: jsx(Input, { value: f.secret, onChange: e => up('secret', e.target.value) })
      }),
      set_h('div', { style: { width: 160 } }, timeoutField)
    )
  } else if (f.type === 'script') {
    actionFields = set_h(
      'div',
      { className: 'flex flex-col gap-3' },
      jsx(SettingsField, {
        label: 'Command (one argument per line)',
        hint: 'Gets the webhook JSON on stdin; may print a JSON response on stdout.',
        children: jsx(SettingsParsed, {
          value: f.command,
          onChange: v => up('command', v),
          format: set_fmtLines,
          parse: set_splitLines,
          multiline: true,
          rows: 4,
          placeholder: '/usr/bin/python3\n/path/to/script.py'
        })
      }),
      set_h('div', { style: { width: 160 } }, timeoutField)
    )
  } else if (f.type === 'reply') {
    actionFields = set_h(
      'div',
      { className: 'flex flex-col gap-3' },
      jsx(SettingsField, {
        label: 'Reply text (template)',
        children: jsx(Textarea, { value: f.text, rows: 4, onChange: e => up('text', e.target.value) })
      }),
      varHelp('text')
    )
  } else if (f.type === 'set_state') {
    actionFields = jsx(SettingsField, {
      label: 'Move the conversation to',
      children: jsx(SettingsSelect, {
        value: f.state,
        onChange: v => up('state', v),
        options: SETTINGS_STATES.map(s => [s, STATE_LABELS[s] || s])
      })
    })
  } else if (f.type === 'add_tags') {
    actionFields = jsx(SettingsField, {
      label: 'Tags (comma separated)',
      children: jsx(SettingsListInput, { value: f.tags, onChange: v => up('tags', v), placeholder: 'vip, billing' })
    })
  } else if (f.type === 'escalate') {
    actionFields = set_h(
      'div',
      { className: 'text-xs', style: set_muted },
      'Raises the conversation priority (escalation) when the rule matches.'
    )
  } else if (f.type === 'takeover') {
    actionFields = set_h(
      'div',
      { className: 'text-xs', style: set_muted },
      'Takes the conversation over: it moves to In progress and agent replies are paused until it is handed back.'
    )
  }

  return set_h(
    'div',
    { className: 'flex flex-col gap-4', style: set_subtle },
    set_h('div', { className: 'text-sm font-semibold' }, rule ? 'Edit rule' : 'New rule'),
    set_h(
      'div',
      { className: 'flex flex-wrap gap-3' },
      set_h(
        'div',
        { style: { flex: '2 1 240px' } },
        jsx(SettingsField, {
          label: 'Name',
          children: jsx(Input, {
            value: f.name,
            onChange: e => up('name', e.target.value),
            placeholder: 'e.g. Reply to new leads'
          })
        })
      ),
      set_h(
        'div',
        { style: { flex: '1 1 180px' } },
        jsx(SettingsField, {
          label: 'Number',
          children: jsx(SettingsSelect, {
            value: f.account_id,
            onChange: v => up('account_id', v),
            options: [['', 'All numbers'], ...accounts.map(a => [String(a.id), a.label])]
          })
        })
      )
    ),
    jsx(SettingsField, {
      label: 'Trigger events',
      hint: 'History imports and messages written by agents or rules never trigger automations.',
      children: set_h(
        'div',
        { className: 'flex flex-wrap gap-x-4 gap-y-1' },
        ...SETTINGS_EVENT_TYPES.map(([v, l]) =>
          jsx(SettingsCheck, {
            key: v,
            checked: f.event_types.includes(v),
            onChange: () => up('event_types', set_toggleIn(f.event_types, v)),
            label: l
          })
        )
      )
    }),
    set_h(
      'div',
      { className: 'flex flex-col gap-3', style: set_card },
      set_h('div', { className: 'text-xs font-semibold' }, 'Conditions (all must match; empty = no restriction)'),
      jsx(SettingsField, {
        label: 'Conversation state is one of',
        children: set_h(
          'div',
          { className: 'flex flex-wrap gap-x-4 gap-y-1' },
          ...SETTINGS_STATES.map(s =>
            jsx(SettingsCheck, {
              key: s,
              checked: f.states.includes(s),
              onChange: () => up('states', set_toggleIn(f.states, s)),
              label: STATE_LABELS[s] || s
            })
          )
        )
      }),
      set_h(
        'div',
        { className: 'flex flex-wrap gap-3' },
        set_h(
          'div',
          { style: { flex: '1 1 160px' } },
          jsx(SettingsField, {
            label: 'Agent active',
            children: jsx(SettingsSelect, {
              value: f.agent_active,
              onChange: v => up('agent_active', v),
              options: triOptions(['Yes (agent active)', 'No (taken over)'])
            })
          })
        ),
        set_h(
          'div',
          { style: { flex: '1 1 160px' } },
          jsx(SettingsField, {
            label: 'Business hours',
            children: jsx(SettingsSelect, {
              value: f.business_hours,
              onChange: v => up('business_hours', v),
              options: [
                ['', 'Any'],
                ['in', 'Inside business hours'],
                ['out', 'Outside business hours']
              ]
            })
          })
        ),
        set_h(
          'div',
          { style: { flex: '1 1 160px' } },
          jsx(SettingsField, {
            label: 'First message',
            children: jsx(SettingsSelect, {
              value: f.first_message,
              onChange: v => up('first_message', v),
              options: triOptions(['Only the first message', 'Not the first message'])
            })
          })
        )
      ),
      jsx(SettingsField, {
        label: 'Direction',
        children: set_h(
          'div',
          { className: 'flex gap-4' },
          jsx(SettingsCheck, {
            checked: f.directions.includes('in'),
            onChange: () => up('directions', set_toggleIn(f.directions, 'in')),
            label: 'Inbound'
          }),
          jsx(SettingsCheck, {
            checked: f.directions.includes('out'),
            onChange: () => up('directions', set_toggleIn(f.directions, 'out')),
            label: 'Outbound'
          })
        )
      }),
      jsx(SettingsField, {
        label: 'Text contains any of (comma separated, case-insensitive)',
        children: jsx(SettingsListInput, {
          value: f.text_contains,
          onChange: v => up('text_contains', v),
          placeholder: 'price, quote, urgent'
        })
      }),
      jsx(SettingsField, {
        label: 'Text matches regex',
        children: jsx(Input, {
          value: f.text_regex,
          onChange: e => up('text_regex', e.target.value),
          placeholder: '^(hi|hello)\\b'
        })
      }),
      jsx(SettingsField, {
        label: 'Conversation has any of these tags (comma separated)',
        children: jsx(SettingsListInput, { value: f.tags_any, onChange: v => up('tags_any', v), placeholder: 'vip' })
      })
    ),
    set_h(
      'div',
      { className: 'flex flex-col gap-3', style: set_card },
      set_h('div', { className: 'text-xs font-semibold' }, 'Action'),
      jsx(SettingsField, {
        label: 'Action type',
        children: jsx(SettingsSelect, {
          value: f.type,
          onChange: v =>
            setF(prev => ({
              ...prev,
              type: v,
              timeout: String(SETTINGS_TIMEOUTS[v] || prev.timeout),
              prompt: v === 'hermes' && !prev.prompt.trim() ? SETTINGS_DEFAULT_PROMPT : prev.prompt
            })),
          options: SETTINGS_ACTIONS
        })
      }),
      actionFields,
      isReply
        ? jsx(SettingsField, {
            label: 'Reply mode',
            hint: modeInfo ? modeInfo[2] + ' Skipped when the conversation is taken over (agent inactive).' : '',
            children: jsx(SettingsSegmented, {
              value: f.reply_mode,
              onChange: v => up('reply_mode', v),
              options: SETTINGS_REPLY_MODES.map(m => [m[0], m[1]])
            })
          })
        : null
    ),
    jsx(SettingsToggle, {
      checked: f.stop_after_match,
      onChange: v => up('stop_after_match', v),
      label: 'Stop after match',
      hint: 'When this rule matches, later rules (in list order) are not evaluated for the same event.'
    }),
    jsx(SettingsToggle, { checked: f.enabled, onChange: v => up('enabled', v), label: 'Enabled' }),
    error ? set_h('div', { className: 'text-xs', style: set_errorBox }, error) : null,
    set_h(
      'div',
      { className: 'flex gap-2' },
      jsx(Button, { size: 'sm', loading: busy, onClick: save, children: rule ? 'Save rule' : 'Create rule' }),
      jsx(Button, { size: 'sm', variant: 'outline', disabled: busy, onClick: onCancel, children: 'Cancel' })
    )
  )
}

function SettingsRunsTable({ rules }) {
  const q = useApi('automation-runs', '/automation-runs?limit=50')
  const [busyId, setBusyId] = useState(null)
  const runs = (q.data && q.data.runs) || []
  const names = {}
  for (const r of rules) {
    names[r.id] = r.name
  }
  const retry = async id => {
    setBusyId(id)
    await act('/automation-runs/' + id + '/retry', undefined)
    setBusyId(null)
  }
  const cell = { padding: '4px 8px', textAlign: 'left', verticalAlign: 'top' }
  return jsx(SettingsCard, {
    title: 'Recent runs',
    desc: 'The 50 most recent automation runs. Failed runs are retried automatically up to 3 times.',
    children:
      q.error && !q.data
        ? jsx(ErrorState, { title: 'Could not load runs', description: errorText(q.error) })
        : !q.data
          ? jsx(Skeleton, { className: 'h-16 w-full' })
          : runs.length === 0
            ? set_h('div', { className: 'text-xs', style: set_muted }, 'No runs yet.')
            : set_h(
                'div',
                { style: { overflowX: 'auto' } },
                set_h(
                  'table',
                  { className: 'text-xs w-full', style: { borderCollapse: 'collapse' } },
                  set_h(
                    'thead',
                    {},
                    set_h(
                      'tr',
                      { style: { ...set_muted, borderBottom: '1px solid var(--ui-stroke-secondary)' } },
                      ...['Run', 'Rule', 'Conversation', 'Status', 'Tries', 'Created', 'Result', ''].map((h, i) =>
                        set_h('th', { key: i, style: { ...cell, fontWeight: 500 } }, h)
                      )
                    )
                  ),
                  set_h(
                    'tbody',
                    {},
                    ...runs.map(run => {
                      const detail = run.error || run.output || ''
                      return set_h(
                        'tr',
                        { key: run.id, style: { borderBottom: '1px solid var(--ui-stroke-tertiary)' } },
                        set_h('td', { style: { ...cell, ...set_muted } }, '#' + run.id),
                        set_h('td', { style: cell }, names[run.rule_id] || '#' + run.rule_id),
                        set_h('td', { style: cell }, run.conversation_id ? '#' + run.conversation_id : '—'),
                        set_h(
                          'td',
                          { style: cell },
                          jsx(SettingsBadge, {
                            variant: SETTINGS_RUN_BADGE[run.status] || 'muted',
                            children: run.status
                          })
                        ),
                        set_h('td', { style: cell }, String(run.attempts)),
                        set_h(
                          'td',
                          { style: { ...cell, whiteSpace: 'nowrap' } },
                          run.created_at ? fmtTime(run.created_at) : ''
                        ),
                        set_h(
                          'td',
                          {
                            style: {
                              ...cell,
                              maxWidth: 280,
                              wordBreak: 'break-word',
                              color: run.error ? 'var(--ui-red)' : 'var(--ui-text-secondary)'
                            },
                            title: detail
                          },
                          set_clip(detail, 120)
                        ),
                        set_h(
                          'td',
                          { style: cell },
                          run.status === 'failed'
                            ? jsx(Button, {
                                size: 'xs',
                                variant: 'outline',
                                loading: busyId === run.id,
                                onClick: () => retry(run.id),
                                children: 'Retry'
                              })
                            : null
                        )
                      )
                    })
                  )
                )
              )
  })
}

function SettingsAutomations({ draft, set }) {
  const rulesQ = useApi('automations', '/automations')
  const accQ = useApi('accounts', '/accounts')
  const [editing, setEditing] = useState(null) // null | 'new' | rule id
  const [testing, setTesting] = useState(null)
  const [confirmDel, setConfirmDel] = useState(null)
  const [busyId, setBusyId] = useState(null)
  const [error, setError] = useState('')
  const rules = (rulesQ.data && rulesQ.data.rules) || []
  const accounts = (accQ.data && accQ.data.accounts) || []
  const accountLabel = id => {
    const a = accounts.find(x => x.id === id)
    return a ? a.label : '#' + id
  }
  const a = (k, fb) => set_getIn(draft, ['automations', k], fb)

  const toggle = async rule => {
    setBusyId(rule.id)
    setError('')
    const res = await set_call('/automations/' + rule.id, 'PUT', { ...set_ruleIn(rule), enabled: !rule.enabled })
    setBusyId(null)
    if (!res.ok) {
      setError(res.error)
    }
  }
  const move = async (i, dir) => {
    const ids = rules.map(r => r.id)
    const j = i + dir
    if (j < 0 || j >= ids.length) {
      return
    }
    ;[ids[i], ids[j]] = [ids[j], ids[i]]
    setBusyId(rules[i].id)
    await act('/automations/reorder', { ids })
    setBusyId(null)
  }
  const remove = async id => {
    setBusyId(id)
    const ok = await act('/automations/' + id, undefined, 'DELETE')
    setBusyId(null)
    if (ok) {
      setConfirmDel(null)
      if (editing === id) {
        setEditing(null)
      }
      if (testing === id) {
        setTesting(null)
      }
    }
  }

  const editingRule = typeof editing === 'number' ? rules.find(r => r.id === editing) : null

  return set_h(
    'div',
    { className: 'flex flex-col gap-3' },
    jsx(SettingsCard, {
      title: 'Automation settings',
      desc: 'Global switches for the dispatcher. Saved together with the other settings.',
      children: set_h(
        'div',
        { className: 'flex flex-col gap-3' },
        jsx(SettingsToggle, {
          checked: a('enabled', false),
          onChange: v => set(['automations', 'enabled'], v),
          label: 'Automations enabled',
          hint: 'When off, no rule is triggered and no run is queued.'
        }),
        set_h(
          'div',
          { className: 'flex flex-wrap gap-3' },
          set_h(
            'div',
            { style: { flex: '1 1 220px' } },
            jsx(SettingsField, {
              label: 'Max runs per conversation per hour',
              hint: 'Rate limit; extra runs are skipped.',
              children: jsx(SettingsNumber, {
                value: a('max_runs_per_conversation_per_hour'),
                onChange: v => set(['automations', 'max_runs_per_conversation_per_hour'], v),
                min: 1
              })
            })
          ),
          set_h(
            'div',
            { style: { flex: '1 1 220px' } },
            jsx(SettingsField, {
              label: 'History messages in prompt',
              hint: 'How many recent messages {history} expands to.',
              children: jsx(SettingsNumber, {
                value: a('history_messages_in_prompt'),
                onChange: v => set(['automations', 'history_messages_in_prompt'], v),
                min: 0
              })
            })
          )
        )
      )
    }),
    set_h(
      'div',
      { className: 'flex items-center justify-between gap-2' },
      set_h('div', { className: 'text-sm font-semibold' }, 'Rules'),
      editing === null ? jsx(Button, { size: 'sm', onClick: () => setEditing('new'), children: 'New rule' }) : null
    ),
    set_h(
      'div',
      { className: 'text-xs', style: set_muted },
      'Rules run top to bottom for each event. Reorder them with the arrows.'
    ),
    error ? set_h('div', { className: 'text-xs', style: set_errorBox }, error) : null,
    editing === 'new'
      ? jsx(SettingsRuleForm, {
          key: 'new',
          rule: null,
          accounts,
          onDone: () => setEditing(null),
          onCancel: () => setEditing(null)
        })
      : null,
    rulesQ.error && !rulesQ.data
      ? jsx(ErrorState, { title: 'Could not load rules', description: errorText(rulesQ.error) })
      : !rulesQ.data
        ? jsx(Skeleton, { className: 'h-16 w-full' })
        : rules.length === 0
          ? set_h('div', { className: 'text-xs', style: set_muted }, 'No automation rules yet.')
          : rules.map((rule, i) => {
              if (editing === rule.id && editingRule) {
                return jsx(SettingsRuleForm, {
                  key: rule.id,
                  rule: editingRule,
                  accounts,
                  onDone: () => setEditing(null),
                  onCancel: () => setEditing(null)
                })
              }
              const busy = busyId === rule.id
              const cond = rule.conditions || {}
              return set_h(
                'div',
                {
                  key: rule.id,
                  className: 'flex flex-col gap-2',
                  style: { ...set_card, opacity: rule.enabled ? 1 : 0.65 }
                },
                set_h(
                  'div',
                  { className: 'flex flex-wrap items-center gap-2' },
                  jsx(SettingsToggle, {
                    checked: rule.enabled,
                    disabled: busy,
                    onChange: () => toggle(rule),
                    label: rule.name
                  }),
                  jsx(SettingsBadge, { variant: 'outline', children: set_ruleSummary(rule) }),
                  jsx(SettingsBadge, {
                    variant: 'muted',
                    children:
                      rule.account_id === null || rule.account_id === undefined
                        ? 'All numbers'
                        : accountLabel(rule.account_id)
                  }),
                  SETTINGS_REPLY_ACTIONS.includes((rule.action || {}).type)
                    ? jsx(SettingsBadge, { variant: 'muted', children: 'reply: ' + rule.reply_mode })
                    : null,
                  rule.stop_after_match
                    ? jsx(SettingsBadge, { variant: 'muted', children: 'stops after match' })
                    : null,
                  set_h(
                    'div',
                    { className: 'flex gap-1 ml-auto' },
                    jsx(Button, {
                      size: 'icon-xs',
                      variant: 'ghost',
                      title: 'Move up',
                      disabled: busy || i === 0,
                      onClick: () => move(i, -1),
                      children: '↑'
                    }),
                    jsx(Button, {
                      size: 'icon-xs',
                      variant: 'ghost',
                      title: 'Move down',
                      disabled: busy || i === rules.length - 1,
                      onClick: () => move(i, 1),
                      children: '↓'
                    })
                  )
                ),
                set_h(
                  'div',
                  { className: 'text-xs', style: set_muted },
                  'On ' +
                    (rule.event_types || []).join(', ') +
                    (Object.keys(cond).length
                      ? ' · ' + Object.keys(cond).length + ' condition' + (Object.keys(cond).length === 1 ? '' : 's')
                      : ' · no conditions')
                ),
                confirmDel === rule.id
                  ? jsx(SettingsConfirm, {
                      message: 'Delete “' + rule.name + '”? Its run history is kept but can no longer be retried.',
                      confirmLabel: 'Delete',
                      busy,
                      onCancel: () => setConfirmDel(null),
                      onConfirm: () => remove(rule.id)
                    })
                  : set_h(
                      'div',
                      { className: 'flex gap-1.5' },
                      jsx(Button, {
                        size: 'xs',
                        variant: 'secondary',
                        onClick: () => {
                          setEditing(rule.id)
                          setTesting(null)
                        },
                        children: 'Edit'
                      }),
                      jsx(Button, {
                        size: 'xs',
                        variant: 'secondary',
                        onClick: () => setTesting(testing === rule.id ? null : rule.id),
                        children: 'Test on conversation'
                      }),
                      jsx(Button, {
                        size: 'xs',
                        variant: 'outline',
                        onClick: () => setConfirmDel(rule.id),
                        children: 'Delete'
                      })
                    ),
                testing === rule.id ? jsx(SettingsRuleTest, { rule, onClose: () => setTesting(null) }) : null
              )
            }),
    jsx(SettingsRunsTable, { rules })
  )
}

// --- Page --------------------------------------------------------------------------------------------

function SettingsSaveBar({ dirty, saving, error, savedAt, onSave, onReset }) {
  return set_h(
    'div',
    {
      className: 'flex flex-col gap-2',
      style: {
        position: 'sticky',
        bottom: 0,
        padding: '10px 0',
        background: 'var(--ui-bg-elevated)',
        borderTop: '1px solid var(--ui-stroke-secondary)'
      }
    },
    error ? set_h('div', { className: 'text-xs', style: set_errorBox }, error) : null,
    set_h(
      'div',
      { className: 'flex items-center gap-2' },
      jsx(Button, { size: 'sm', loading: saving, disabled: !dirty, onClick: onSave, children: 'Save' }),
      jsx(Button, { size: 'sm', variant: 'outline', disabled: !dirty || saving, onClick: onReset, children: 'Reset' }),
      dirty
        ? jsx(SettingsBadge, { variant: 'warn', children: 'Unsaved changes' })
        : savedAt
          ? set_h('span', { className: 'text-xs', style: set_muted }, 'Saved ' + set_ago(Math.floor(savedAt / 1000)))
          : null
    )
  )
}

function SettingsPage() {
  const [sec, setSec] = useState('numbers')
  const q = useApi('settings', '/settings')
  const [state, setState] = useState({ base: null, draft: null })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [savedAt, setSavedAt] = useState(0)

  useEffect(() => {
    if (!q.data) {
      return
    }
    // Adopt server data only when the user has no unsaved edits.
    setState(s => {
      const clean = s.draft === null || set_same(s.draft, s.base)
      return { base: q.data, draft: clean ? set_clone(q.data) : s.draft }
    })
  }, [q.data])

  const draft = state.draft
  const dirty = draft !== null && !set_same(draft, state.base)
  const set = (path, value) => setState(s => ({ ...s, draft: set_setIn(s.draft, path, value) }))
  const reset = () => {
    setError('')
    setState(s => ({ base: s.base, draft: set_clone(s.base) }))
  }
  const save = async () => {
    setSaving(true)
    setError('')
    try {
      const saved = await rest('/settings', { method: 'PUT', body: draft })
      setState({ base: saved, draft: set_clone(saved) })
      setSavedAt(Date.now())
      refresh()
    } catch (err) {
      setError(set_errMsg(err))
    }
    setSaving(false)
  }

  let content
  if (sec === 'numbers') {
    content = jsx(SettingsAccounts, {})
  } else if (draft === null) {
    content = q.error
      ? jsx(ErrorState, { title: 'Backend unreachable', description: errorText(q.error) })
      : jsx(Skeleton, { className: 'h-32 w-full' })
  } else {
    const props = { draft, set }
    const form =
      sec === 'rules'
        ? jsx(SettingsRulesForm, props)
        : sec === 'notifications'
          ? jsx(SettingsNotificationsForm, props)
          : sec === 'privacy'
            ? jsx(SettingsPrivacyForm, props)
            : sec === 'board'
              ? jsx(SettingsBoardForm, props)
              : sec === 'hours'
                ? jsx(SettingsHoursForm, props)
                : jsx(SettingsAutomations, props)
    content = set_h(
      'div',
      { className: 'flex flex-col gap-3' },
      form,
      jsx(SettingsSaveBar, { dirty, saving, error, savedAt, onSave: save, onReset: reset })
    )
  }

  return set_h(
    'div',
    { style: { display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 } },
    set_h(
      'div',
      { className: 'flex flex-col gap-2', style: { padding: '12px 16px 0' } },
      set_h('div', { className: 'text-base font-semibold' }, 'WhatsApp Chat settings'),
      jsx(ServiceBanner, {})
    ),
    set_h(
      'div',
      { style: { display: 'flex', flex: 1, minHeight: 0, marginTop: 8 } },
      set_h(
        'nav',
        {
          className: 'flex flex-col gap-1 shrink-0',
          style: { width: 180, padding: '8px 12px', borderRight: '1px solid var(--ui-stroke-secondary)' }
        },
        ...SETTINGS_SECTIONS.map(([id, label]) =>
          jsx(Button, {
            key: id,
            size: 'sm',
            variant: sec === id ? 'secondary' : 'ghost',
            className: 'justify-start',
            onClick: () => setSec(id),
            children: label
          })
        ),
        SETTINGS_SAVE_SECTIONS.includes(sec) && dirty
          ? set_h(
              'span',
              { className: 'text-xs', style: { color: 'var(--ui-orange)', padding: '4px 10px' } },
              'Unsaved changes'
            )
          : null
      ),
      set_h('div', { style: { flex: 1, minWidth: 0, overflowY: 'auto', padding: '8px 16px 16px' } }, content)
    )
  )
}
