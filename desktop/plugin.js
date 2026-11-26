/**
 * Claude accounts (Anthropic): a status-bar chip + a panel to see how much each account has
 * left, reorder them by dragging, name them and remove them.
 *
 * Usage rule: every chat keeps its own account. If its limit runs out (even mid-task, with
 * subagents working), Hermes moves on its own to the account with the MOST usage left and saves
 * it for that chat. The order (drag) decides which account new chats start with. "Use in this
 * chat" assigns an account to the open chat only.
 *
 * SDK rules: no JSX, only imports from @hermes/plugin-sdk / react / react/jsx-runtime, no fixed
 * theme colors (--ui-* variables). Per-account identity colors follow the same hsl scheme as the
 * app's profileColor.
 *
 * Strings are registered through ctx.i18n (English + Spanish), so the panel follows the app's
 * display.language; without ctx.i18n it falls back to English.
 */
import * as sdk from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const PLUGIN_ID = 'anthropic-cuentas'
const POLL_CLOSED_MS = 5 * 60 * 1000
const POLL_OPEN_MS = 30 * 1000

const host = sdk.host
const { useValue, atom, Popover, PopoverContent, PopoverTrigger, STATUSBAR_AREAS } = sdk
const icons = sdk.icons || {}

const c = {
  primary: 'var(--ui-text-primary)',
  secondary: 'var(--ui-text-secondary)',
  tertiary: 'var(--ui-text-tertiary)',
  quaternary: 'var(--ui-text-quaternary)',
  red: 'var(--ui-red)',
  yellow: 'var(--ui-yellow)',
  green: 'var(--ui-green)',
  accent: 'var(--ui-accent)',
  stroke: 'var(--ui-stroke-secondary)',
  soft: 'var(--ui-bg-quaternary, color-mix(in srgb, var(--ui-stroke-secondary) 35%, transparent))'
}

// ------------------------------------------------------------------ strings

const MESSAGES = {
  en: {
    title: 'Claude accounts',
    days: 'Sun,Mon,Tue,Wed,Thu,Fri,Sat',
    now: 'now',
    inMin: n => `in ${n} min`,
    today: time => `today ${time}`,
    tomorrow: time => `tomorrow ${time}`,
    dayAt: (day, date, time) => `${day} ${date}, ${time}`,
    noData: 'no data',
    usedPct: n => `${n}% used`,
    renews: when => `renews ${when}`,
    tagDuplicate: 'Duplicate',
    tagError: 'Not responding',
    tagLimit: 'Exhausted',
    tagPaused: 'Paused',
    tagThisChat: 'This chat',
    tagBackup: 'Backup',
    tagBackupTip: "If this chat's account runs out, it moves to this one (the one with most usage left)",
    tagNewChats: 'New chats',
    tagNewChatsTip: 'New chats start with this one',
    tagAvailable: 'Available',
    renamed: v => `Now called “${v}”.`,
    nameRemoved: 'Name removed.',
    orderSaved: 'Order saved. Applies from your next message.',
    noQuota: 'No quota',
    backAt: (why, when) => `${why} · back ${when}`,
    dragTip: 'Drag to reorder (or use the ↑ ↓ arrows)',
    moveLabel: (name, pos, total) => `Move ${name}. Position ${pos} of ${total}`,
    clickToName: 'Click to name it',
    nameIt: 'Name it',
    chatsTip: 'Chats that used it in the last 6 hours',
    chats: n => (n === 1 ? '1 chat' : `${n} chats`),
    duplicateOf: name => `This is the same account as “${name}”: it shares its limit, so it gives no extra usage; it only adds failed attempts. You can remove it without losing anything.`,
    limit5h: '5-hour limit',
    limitWeek: 'Weekly limit',
    staleData: "Previous data: Anthropic didn't answer this time.",
    confirmRemoveDup: 'Remove this duplicate access? The account stays available through the other entry.',
    confirmRemove: 'Remove this account from Hermes? To use it again you will have to reconnect it.',
    cancel: 'Cancel',
    removed: name => `Removed: ${name}. A backup of auth.json was kept.`,
    removing: 'Removing…',
    yesRemove: 'Yes, remove',
    useHereTip: 'Only the open chat uses this account, from its next message. Other chats do not change.',
    assignedButExhausted: 'Assigned to this chat, but it is exhausted: if it stays that way, Hermes switches to the one with most usage left.',
    chatWillUse: name => `This chat will use ${name} from your next message.`,
    applying: 'Applying…',
    useHere: 'Use in this chat',
    removeTip: 'Remove this account from Hermes',
    remove: 'Remove',
    refreshTip: 'Ask Anthropic again',
    refreshing: 'Refreshing…',
    refresh: 'Refresh',
    chatUses: 'This chat uses ',
    newChatStarts: 'A new chat will start with ',
    leftPct: n => ` · ${n}% left`,
    pickedByYou: 'You picked it for this chat.',
    switchedOnItsOwn: why => `Switched on its own: ${why}.`,
    ifRunsOut: (name, left) => `If it runs out, it continues with ${name}${left}.`,
    leftParen: n => ` (${n}% left)`,
    noBackup: 'If it runs out, no other account has usage left.',
    intro: 'Each chat keeps its own account. If it runs out, even while you are away and subagents are working, Hermes moves on its own to the one with most usage left and continues the task. The order (drag from the dots) decides which one new chats start with.',
    dups: (real, entries, dups) => `You have ${real} real accounts in ${entries} entries: ${dups === 1 ? 'one is repeated' : `${dups} are repeated`}. Repeated ones give no extra usage.`,
    readingAccounts: 'Reading accounts…',
    noDataYet: 'No data yet.',
    dataAge: a => `Data from ${a}. `,
    momentAgo: 'a moment ago',
    secondsAgo: n => `${n} s ago`,
    minutesAgo: n => `${n} min ago`,
    manualNote: "Manual changes apply from that chat's next message.",
    chipExhausted: when => `exhausted · back ${when}`,
    chipLeft: n => `${n}% left`,
    tipChatUses: 'This chat uses',
    tipNewChat: 'A new chat will start with',
    tipClick: 'Click to see and reorder your accounts.',
    // status codes from the backend (status.code / error_code)
    status: {
      limit_5h: 'The 5-hour limit ran out',
      limit_week: 'The weekly limit ran out',
      paused: 'Hermes paused it after a limit warning',
      no_token: 'No token',
      expired: 'The session expired: reconnect it',
      offline: 'No connection'
    },
    // reasons stored in sesiones.json
    reason: {
      manual: 'picked by hand',
      exhausted: "this chat's account ran out",
      gone: "this chat's account is no longer in Hermes",
      midtask: 'the previous account ran out mid-task'
    }
  },
  es: {
    title: 'Cuentas de Claude',
    days: 'dom,lun,mar,mié,jue,vie,sáb',
    now: 'ya',
    inMin: n => `en ${n} min`,
    today: time => `hoy ${time}`,
    tomorrow: time => `mañana ${time}`,
    dayAt: (day, date, time) => `${day} ${date}, ${time}`,
    noData: 'sin dato',
    usedPct: n => `${n}% usado`,
    renews: when => `se renueva ${when}`,
    tagDuplicate: 'Duplicada',
    tagError: 'No responde',
    tagLimit: 'Agotada',
    tagPaused: 'En pausa',
    tagThisChat: 'Este chat',
    tagBackup: 'Respaldo',
    tagBackupTip: 'Si la cuenta de este chat se agota, pasa a esta (la de más uso disponible)',
    tagNewChats: 'Chats nuevos',
    tagNewChatsTip: 'Los chats nuevos empiezan con esta',
    tagAvailable: 'Disponible',
    renamed: v => `Ahora se llama «${v}».`,
    nameRemoved: 'Nombre quitado.',
    orderSaved: 'Orden guardado. Aplica desde tu siguiente mensaje.',
    noQuota: 'Sin cupo',
    backAt: (why, when) => `${why} · vuelve ${when}`,
    dragTip: 'Arrastra para cambiar el orden (o usa las flechas ↑ ↓)',
    moveLabel: (name, pos, total) => `Mover ${name}. Posición ${pos} de ${total}`,
    clickToName: 'Clic para ponerle nombre',
    nameIt: 'Ponerle nombre',
    chatsTip: 'Chats que la usaron en las últimas 6 horas',
    chats: n => (n === 1 ? '1 chat' : `${n} chats`),
    duplicateOf: name => `Es la misma cuenta que «${name}»: comparte su límite, así que no te da más uso; solo suma intentos fallidos. Puedes quitarla sin perder nada.`,
    limit5h: 'Límite de 5 horas',
    limitWeek: 'Límite semanal',
    staleData: 'Dato anterior: Anthropic no respondió esta vez.',
    confirmRemoveDup: '¿Quitar este acceso duplicado? La cuenta sigue disponible por la otra entrada.',
    confirmRemove: '¿Quitar esta cuenta de Hermes? Para volver a usarla hay que conectarla otra vez.',
    cancel: 'Cancelar',
    removed: name => `Quitada: ${name}. Dejé un respaldo de auth.json.`,
    removing: 'Quitando…',
    yesRemove: 'Sí, quitar',
    useHereTip: 'Que solo el chat abierto use esta cuenta, desde su siguiente mensaje. Los demás chats no cambian.',
    assignedButExhausted: 'Asignada a este chat, pero está agotada: si sigue así, Hermes la cambia sola por la de más uso.',
    chatWillUse: name => `Este chat usará ${name} desde tu siguiente mensaje.`,
    applying: 'Aplicando…',
    useHere: 'Usar en este chat',
    removeTip: 'Quitar esta cuenta de Hermes',
    remove: 'Quitar',
    refreshTip: 'Volver a preguntar a Anthropic',
    refreshing: 'Actualizando…',
    refresh: 'Actualizar',
    chatUses: 'Este chat usa ',
    newChatStarts: 'Un chat nuevo empezará con ',
    leftPct: n => ` · le queda ${n}%`,
    pickedByYou: 'La elegiste tú para este chat.',
    switchedOnItsOwn: why => `Cambió sola: ${why}.`,
    ifRunsOut: (name, left) => `Si se agota, sigue sola con ${name}${left}.`,
    leftParen: n => ` (le queda ${n}%)`,
    noBackup: 'Si se agota, no hay otra cuenta con uso disponible.',
    intro: 'Cada chat se queda con su cuenta. Si se le acaba, aunque estés fuera y haya subagentes trabajando, Hermes pasa sola a la que tenga más uso disponible y sigue la tarea. El orden (arrastra desde los puntitos) decide con cuál empiezan los chats nuevos.',
    dups: (real, entries, dups) => `Tienes ${real} cuentas reales en ${entries} entradas: ${dups === 1 ? 'una está repetida' : `${dups} están repetidas`}. Las repetidas no dan más uso.`,
    readingAccounts: 'Leyendo cuentas…',
    noDataYet: 'Sin datos todavía.',
    dataAge: a => `Datos de ${a}. `,
    momentAgo: 'hace un momento',
    secondsAgo: n => `hace ${n} s`,
    minutesAgo: n => `hace ${n} min`,
    manualNote: 'Los cambios a mano aplican desde el siguiente mensaje de ese chat.',
    chipExhausted: when => `agotada · vuelve ${when}`,
    chipLeft: n => `le queda ${n}%`,
    tipChatUses: 'Este chat usa',
    tipNewChat: 'Un chat nuevo empezará con',
    tipClick: 'Clic para ver y ordenar tus cuentas.',
    status: {
      limit_5h: 'Se acabó el límite de 5 horas',
      limit_week: 'Se acabó el límite semanal',
      paused: 'Hermes la pausó tras un aviso de límite',
      no_token: 'Sin token',
      expired: 'La sesión venció: vuelve a conectarla',
      offline: 'Sin conexión'
    },
    reason: {
      manual: 'elegida a mano',
      exhausted: 'la cuenta de este chat se agotó',
      gone: 'la cuenta de este chat ya no está en Hermes',
      midtask: 'la anterior se agotó a media tarea'
    }
  }
}

// Reasons written by v0.3 and earlier (Spanish sentences) map onto today's codes.
const LEGACY_REASONS = {
  'elegida a mano': 'manual',
  'la cuenta de este chat se agotó': 'exhausted',
  'la cuenta de este chat ya no está en Hermes': 'gone',
  'la anterior se agotó a media tarea': 'midtask'
}

function lookup(bundle, key) {
  return key.split('.').reduce((node, part) => (node && typeof node === 'object' ? node[part] : undefined), bundle)
}

// English translator, used until ctx.i18n is wired up (or on hosts without it).
function translateEnglish(key, ...args) {
  const v = lookup(MESSAGES.en, key)
  if (typeof v === 'function') return v(...args)
  return typeof v === 'string' ? v : key
}

let translate = translateEnglish

function t(key, ...args) {
  return translate(key, ...args)
}

function hasKey(key) {
  return lookup(MESSAGES.en, key) !== undefined
}

const $locale = atom(0)          // bumps on a language switch so the UI re-renders

function statusWhy(s) {
  if (!s) return ''
  if (s.code && hasKey(`status.${s.code}`)) return t(`status.${s.code}`)
  return s.why || ''
}

function reasonCode(reason) {
  return LEGACY_REASONS[reason] || reason
}

function reasonText(reason) {
  if (!reason) return ''
  const code = reasonCode(reason)
  return hasKey(`reason.${code}`) ? t(`reason.${code}`) : reason
}

const $data = atom(null)
const $error = atom(null)
const $open = atom(false)
const $loading = atom(false)
const $busy = atom(null)          // id of the account with an action in flight
const $now = atom(Date.now())

let rest = null

// ------------------------------------------------------------------ data

function sessionId() {
  try {
    const st = host && host.state
    const a = st && st.focusedStoredSessionId && st.focusedStoredSessionId.get()
    return a || (st && st.activeSessionId && st.activeSessionId.get()) || null
  } catch {
    return null
  }
}

async function load(fresh) {
  if (!rest || $loading.get()) return
  $loading.set(true)
  try {
    const q = []
    if (fresh) q.push('fresh=1')
    const sid = sessionId()
    if (sid) q.push('session=' + encodeURIComponent(sid))
    const data = await rest('/accounts' + (q.length ? '?' + q.join('&') : ''))
    $data.set(data)
    $error.set(null)
  } catch (err) {
    $error.set(errText(err))
  } finally {
    $loading.set(false)
  }
}

function errText(err) {
  const raw = String((err && (err.detail || err.message)) || err)
  const m = raw.match(/"detail"\s*:\s*"([^"]+)"/)
  return m ? m[1] : raw
}

async function act(path, body, okMessage, busyId) {
  if (!rest) return false
  $busy.set(busyId || path)
  try {
    const res = await rest(path, { method: 'POST', body })
    if (okMessage) host.notify({ kind: 'success', message: typeof okMessage === 'function' ? okMessage(res) : okMessage })
    $loading.set(false)
    await load(false)
    return res
  } catch (err) {
    host.notify({ kind: 'error', message: errText(err) })
    await load(false)
    return false
  } finally {
    $busy.set(null)
  }
}

function saveOrder(ids) {
  const d = $data.get()
  if (d && d.accounts) {
    // reflect it right away, without waiting for the server
    const byId = Object.fromEntries(d.accounts.map(a => [a.id, a]))
    $data.set({ ...d, accounts: ids.map((id, i) => ({ ...byId[id], priority: i })).filter(a => a.id) })
  }
  return act('/order', { ids }, t('orderSaved'), 'order')
}

// ------------------------------------------------------------------ formatting

function shortName(a) {
  if (!a) return 'Claude'
  if (a.alias) return a.alias
  if (!a.email) return a.label || a.id
  return String(a.email).split('@')[0]
}

const HUES = [212, 32, 150, 282, 352, 190, 58, 252]

function identityColors(accounts) {
  // stable per account (alphabetical by email), not by position: dragging does not change colors
  const emails = [...new Set((accounts || []).map(a => a.email).filter(Boolean))].sort()
  const map = {}
  emails.forEach((e, i) => { map[e] = `hsl(${HUES[i % HUES.length]} 68% 58%)` })
  return map
}

function colorFor(a, colors) {
  return (a && a.email && colors[a.email]) || c.quaternary
}

function clock(d) {
  let h = d.getHours()
  const m = String(d.getMinutes()).padStart(2, '0')
  const ap = h >= 12 ? 'pm' : 'am'
  h = h % 12 || 12
  return `${h}:${m} ${ap}`
}

function whenText(epochSec, nowMs) {
  if (!epochSec) return ''
  const d = new Date(epochSec * 1000)
  const now = new Date(nowMs)
  const diffMin = Math.round((d.getTime() - nowMs) / 60000)
  if (diffMin <= 0) return t('now')
  if (diffMin < 60) return t('inMin', diffMin)
  const sameDay = d.toDateString() === now.toDateString()
  if (sameDay) return t('today', clock(d))
  const tomorrow = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1)
  if (d.toDateString() === tomorrow.toDateString()) return t('tomorrow', clock(d))
  const days = t('days').split(',')
  return t('dayAt', days[d.getDay()], d.getDate(), clock(d))
}

function isoToSec(iso) {
  if (!iso) return null
  const ms = new Date(iso).getTime()
  return Number.isFinite(ms) ? ms / 1000 : null
}

function leftPct(a) {
  if (!a) return null
  const used = [a.five_hour, a.seven_day].filter(Boolean).map(w => w.used_pct)
  if (!used.length) return null
  return Math.max(0, Math.round(100 - Math.max(...used)))
}

function toneFor(pctLeft) {
  if (pctLeft === null) return c.quaternary
  if (pctLeft <= 10) return c.red
  if (pctLeft <= 30) return c.yellow
  return c.green
}

function usedTone(used) {
  if (used === null) return c.quaternary
  if (used >= 90) return c.red
  if (used >= 70) return c.yellow
  return c.green
}

// ------------------------------------------------------------------ pieces

function Icon({ name, size = 13 }) {
  const Comp = icons[name]
  if (!Comp) return null
  return jsx(Comp, { size, style: { width: size, height: size }, 'aria-hidden': true })
}

function Swatch({ color, size = 9 }) {
  return jsx('span', {
    'aria-hidden': true,
    style: { width: size, height: size, borderRadius: 3, background: color, display: 'inline-block', flex: 'none' }
  })
}

function GripGlyph() {
  return jsxs('svg', {
    width: 8, height: 14, viewBox: '0 0 8 14', 'aria-hidden': true,
    children: [0, 1, 2].flatMap(r => [0, 1].map(col =>
      jsx('circle', { cx: 2 + col * 4, cy: 2.5 + r * 4.5, r: 1.2, fill: 'currentColor' }, `${r}-${col}`)))
  })
}

function Tag({ children, tone, title }) {
  const color = tone || c.tertiary
  return jsx('span', {
    title,
    style: {
      fontSize: 10, lineHeight: '16px', padding: '0 6px', borderRadius: 8, whiteSpace: 'nowrap',
      color, background: `color-mix(in srgb, ${color} 14%, transparent)`
    },
    children
  })
}

function TextButton({ onClick, children, title, disabled, danger, strong }) {
  return jsx('button', {
    type: 'button', title, disabled: !!disabled,
    onClick: e => { e.stopPropagation(); if (!disabled) onClick(e) },
    style: {
      display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 11, padding: '2px 7px', borderRadius: 4,
      border: 'none', cursor: disabled ? 'default' : 'pointer', opacity: disabled ? 0.5 : 1,
      color: danger ? c.red : strong ? c.primary : c.secondary,
      background: strong ? c.soft : 'transparent', whiteSpace: 'nowrap'
    },
    children
  })
}

function Meter({ label, win, nowMs }) {
  const used = win ? Math.max(0, Math.min(100, win.used_pct)) : null
  const reset = win ? isoToSec(win.resets_at) : null
  return jsxs('div', {
    style: { flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 3 },
    children: [
      jsxs('div', { style: { display: 'flex', alignItems: 'baseline', gap: 6, fontSize: 10.5 }, children: [
        jsx('span', { style: { color: c.tertiary }, children: label }),
        jsx('span', { style: { flex: 1 } }),
        jsx('span', {
          style: { color: used !== null && used >= 99.5 ? c.red : c.primary, fontVariantNumeric: 'tabular-nums' },
          children: used === null ? t('noData') : t('usedPct', Math.round(used))
        })
      ] }),
      jsx('div', {
        style: { height: 4, borderRadius: 2, background: `color-mix(in srgb, ${c.stroke} 70%, transparent)`, overflow: 'hidden' },
        children: jsx('div', { style: { width: `${used === null ? 0 : used}%`, height: '100%', background: usedTone(used), borderRadius: 2 } })
      }),
      jsx('span', {
        style: { fontSize: 10, color: c.quaternary, minHeight: 13 },
        children: reset && used !== null && used > 0 ? t('renews', whenText(reset, nowMs)) : ''
      })
    ]
  })
}

function StateTag({ account, inUse, isNext, isBest }) {
  const s = account.status || {}
  if (account.duplicate_of) return jsx(Tag, { tone: c.yellow, children: t('tagDuplicate') })
  if (s.state === 'error') return jsx(Tag, { tone: c.red, title: statusWhy(s), children: t('tagError') })
  if (s.state === 'limit') return jsx(Tag, { tone: c.red, title: statusWhy(s), children: t('tagLimit') })
  if (s.state === 'paused') return jsx(Tag, { tone: c.yellow, title: statusWhy(s), children: t('tagPaused') })
  if (inUse) return jsx(Tag, { tone: c.accent, children: t('tagThisChat') })
  if (isBest) return jsx(Tag, { tone: c.green, title: t('tagBackupTip'), children: t('tagBackup') })
  if (isNext) return jsx(Tag, { tone: c.green, title: t('tagNewChatsTip'), children: t('tagNewChats') })
  return jsx(Tag, { tone: c.tertiary, children: t('tagAvailable') })
}

function AliasEditor({ account, onDone }) {
  const [value, setValue] = useState(account.alias || '')
  const ref = useRef(null)
  useEffect(() => { if (ref.current) { ref.current.focus(); ref.current.select() } }, [])
  const save = () => {
    onDone()
    const v = value.trim()
    if (v === (account.alias || '')) return
    act('/alias', { email: account.email, alias: v || null }, v ? t('renamed', v) : t('nameRemoved'), account.id)
  }
  return jsx('input', {
    ref, value, maxLength: 24, placeholder: String(account.email || '').split('@')[0],
    onChange: e => setValue(e.target.value),
    onKeyDown: e => { if (e.key === 'Enter') save(); if (e.key === 'Escape') onDone() },
    onBlur: save,
    onPointerDown: e => e.stopPropagation(),
    style: {
      fontSize: 12.5, fontWeight: 600, color: c.primary, background: 'transparent', minWidth: 0, width: 150,
      border: `1px solid ${c.accent}`, borderRadius: 4, padding: '1px 5px', outline: 'none'
    }
  })
}

function AccountCard({ account, index, total, colors, nowMs, inUse, isNext, isBest, sid, dragging, dupName, rowRef, onGripDown, onMove }) {
  const busy = useValue($busy)
  const [editing, setEditing] = useState(false)
  const [confirm, setConfirm] = useState(false)
  const s = account.status || {}
  const limitLine = s.state === 'limit' || s.state === 'paused'
    ? t('backAt', statusWhy(s) || t('noQuota'), whenText(s.until, nowMs))
    : s.state === 'error' ? statusWhy(s) : null
  const name = shortName(account)
  return jsxs('div', {
    ref: rowRef,
    style: {
      position: 'relative', display: 'flex', gap: 8, padding: '10px 10px 9px 4px', borderRadius: 6,
      background: dragging ? c.soft : inUse ? `color-mix(in srgb, ${c.accent} 7%, transparent)` : 'transparent',
      boxShadow: dragging ? `0 0 0 1px ${c.accent}` : 'none',
      opacity: account.duplicate_of && !dragging ? 0.85 : 1,
      transition: 'background 120ms'
    },
    children: [
      jsx('button', {
        type: 'button',
        title: t('dragTip'),
        'aria-label': t('moveLabel', name, index + 1, total),
        onPointerDown: e => onGripDown(e, account.id),
        onKeyDown: e => {
          if (e.key === 'ArrowUp' && index > 0) { e.preventDefault(); onMove(account.id, index - 1) }
          if (e.key === 'ArrowDown' && index < total - 1) { e.preventDefault(); onMove(account.id, index + 1) }
        },
        style: {
          flex: 'none', width: 18, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4, paddingTop: 2,
          border: 'none', background: 'transparent', color: c.quaternary, cursor: dragging ? 'grabbing' : 'grab',
          touchAction: 'none'
        },
        children: [
          jsx('span', { style: { fontSize: 10, color: c.tertiary, fontVariantNumeric: 'tabular-nums' }, children: index + 1 }),
          jsx(GripGlyph, {})
        ]
      }),
      jsxs('div', { style: { flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 6 }, children: [
        jsxs('div', { style: { display: 'flex', alignItems: 'center', gap: 7, minWidth: 0 }, children: [
          jsx(Swatch, { color: colorFor(account, colors), size: 10 }),
          editing
            ? jsx(AliasEditor, { account, onDone: () => setEditing(false) })
            : jsx('span', {
              title: account.email ? t('clickToName') : undefined,
              onClick: () => account.email && setEditing(true),
              style: {
                fontSize: 12.5, fontWeight: 600, color: c.primary, cursor: account.email ? 'text' : 'default',
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0
              },
              children: name
            }),
          !editing && account.email ? jsx('button', {
            type: 'button', title: t('nameIt'), 'aria-label': t('nameIt'),
            onClick: () => setEditing(true),
            style: { border: 'none', background: 'transparent', color: c.quaternary, cursor: 'pointer', padding: 0, display: 'inline-flex' },
            children: jsx(Icon, { name: 'Pencil', size: 11 })
          }) : null,
          account.plan ? jsx('span', { style: { fontSize: 10, color: c.tertiary }, children: account.plan === 'max' ? 'Max' : 'Pro' }) : null,
          jsx('span', { style: { flex: 1 } }),
          account.chats > 0 && !inUse ? jsx('span', { style: { fontSize: 10, color: c.tertiary, whiteSpace: 'nowrap' },
            title: t('chatsTip'), children: t('chats', account.chats) }) : null,
          jsx(StateTag, { account, inUse, isNext, isBest })
        ] }),
        jsx('div', {
          style: { fontSize: 10.5, color: c.quaternary, marginTop: -3, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
          title: account.email || account.label,
          children: account.email || account.label
        }),
        account.duplicate_of
          ? jsx('div', {
            style: { fontSize: 11, color: c.secondary, lineHeight: 1.4 },
            children: t('duplicateOf', dupName)
          })
          : jsxs('div', { style: { display: 'flex', gap: 14 }, children: [
            jsx(Meter, { label: t('limit5h'), win: account.five_hour, nowMs }),
            jsx(Meter, { label: t('limitWeek'), win: account.seven_day, nowMs })
          ] }),
        limitLine && !account.duplicate_of
          ? jsx('div', { style: { fontSize: 11, color: s.state === 'paused' ? c.yellow : c.red, marginTop: -2 }, children: limitLine })
          : null,
        account.stale ? jsx('div', { style: { fontSize: 10, color: c.quaternary }, children: t('staleData') }) : null,
        confirm
          ? jsxs('div', {
            style: { display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', padding: '6px 8px', borderRadius: 5,
              background: `color-mix(in srgb, ${c.red} 9%, transparent)` },
            children: [
              jsx('span', { style: { fontSize: 11, color: c.primary, flex: 1, minWidth: 160 },
                children: account.duplicate_of ? t('confirmRemoveDup') : t('confirmRemove') }),
              jsx(TextButton, { onClick: () => setConfirm(false), children: t('cancel') }),
              jsx(TextButton, {
                danger: true, strong: true, disabled: busy === account.id,
                onClick: () => act('/remove', { entry_id: account.id }, t('removed', name), account.id),
                children: busy === account.id ? t('removing') : t('yesRemove')
              })
            ]
          })
          : jsxs('div', { style: { display: 'flex', alignItems: 'center', gap: 2, marginLeft: -7, marginTop: -2 }, children: [
            sid && !inUse && !account.duplicate_of && s.state !== 'limit' ? jsx(TextButton, {
              title: t('useHereTip'),
              disabled: !!busy,
              onClick: () => act('/session', { session_id: sid, entry_id: account.id }, r => r && r.blocked_until
                ? t('assignedButExhausted')
                : t('chatWillUse', name), account.id),
              children: [jsx(Icon, { name: 'Check', size: 12 }), busy === account.id ? t('applying') : t('useHere')]
            }) : null,
            jsx('span', { style: { flex: 1 } }),
            jsx(TextButton, {
              danger: true, title: t('removeTip'), disabled: !!busy || total <= 1,
              onClick: () => setConfirm(true),
              children: [jsx(Icon, { name: 'Trash2', size: 12 }), t('remove')]
            })
          ] })
      ] })
    ]
  })
}

function AccountList({ data, nowMs }) {
  const accounts = data.accounts || []
  const colors = identityColors(accounts)
  const [drag, setDrag] = useState(null)        // { id, order: [...] }
  const rows = useRef({})
  const order = drag ? drag.order : accounts.map(a => a.id)
  const byId = Object.fromEntries(accounts.map(a => [a.id, a]))
  const sessEntry = data.session && data.session.entry_id
  const sid = sessionId()
  const names = Object.fromEntries(accounts.map(a => [a.id, shortName(a)]))

  const move = (id, to) => {
    const ids = accounts.map(a => a.id).filter(x => x !== id)
    ids.splice(to, 0, id)
    saveOrder(ids)
  }

  const onGripDown = (e, id) => {
    if (e.button !== undefined && e.button !== 0) return
    e.preventDefault()
    const start = accounts.map(a => a.id)
    let current = start
    setDrag({ id, order: start })
    const onMoveEv = ev => {
      const y = ev.clientY
      const others = current.filter(x => x !== id)
      let pos = others.length
      for (let i = 0; i < others.length; i++) {
        const el = rows.current[others[i]]
        if (!el) continue
        const r = el.getBoundingClientRect()
        if (y < r.top + r.height / 2) { pos = i; break }
      }
      const next = [...others.slice(0, pos), id, ...others.slice(pos)]
      if (next.join() !== current.join()) {
        current = next
        setDrag({ id, order: next })
      }
    }
    const onUp = () => {
      window.removeEventListener('pointermove', onMoveEv)
      window.removeEventListener('pointerup', onUp)
      window.removeEventListener('pointercancel', onUp)
      setDrag(null)
      if (current.join() !== start.join()) saveOrder(current)
    }
    window.addEventListener('pointermove', onMoveEv)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onUp)
  }

  return jsx('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 2, userSelect: drag ? 'none' : 'auto' },
    children: order.map((id, i) => {
      const a = byId[id]
      if (!a) return null
      return jsx(AccountCard, {
        account: a, index: i, total: order.length, colors, nowMs,
        inUse: sessEntry === a.id, isNext: !sessEntry && data.next_id === a.id,
        isBest: !!sessEntry && data.best_id === a.id && sessEntry !== a.id, sid,
        dragging: drag && drag.id === a.id,
        dupName: a.duplicate_of ? names[a.duplicate_of] : null,
        rowRef: el => { rows.current[a.id] = el },
        onGripDown, onMove: move
      }, a.id)
    })
  })
}

function chatAccount(data) {
  if (!data || !data.accounts || !data.accounts.length) return { account: null, kind: null }
  const sid = data.session && data.session.entry_id
  const inChat = sid && data.accounts.find(a => a.id === sid)
  if (inChat) return { account: inChat, kind: 'chat' }
  const next = data.accounts.find(a => a.id === data.next_id)
  return { account: next || data.accounts[0], kind: 'next' }
}

function ageText(age) {
  if (age < 5) return t('momentAgo')
  if (age < 90) return t('secondsAgo', age)
  return t('minutesAgo', Math.round(age / 60))
}

function Panel() {
  useValue($locale)
  const data = useValue($data)
  const error = useValue($error)
  const loading = useValue($loading)
  const nowMs = useValue($now)
  const { account, kind } = chatAccount(data)
  const best = data && data.accounts ? data.accounts.find(a => a.id === data.best_id) : null
  const colors = identityColors(data && data.accounts)
  const dups = data && data.accounts ? data.accounts.filter(a => a.duplicate_of).length : 0
  const real = data && data.accounts ? new Set(data.accounts.map(a => a.email || a.id)).size : 0
  const age = data && data.now ? Math.max(0, Math.round((nowMs - data.now * 1000) / 1000)) : null
  const reason = data && data.session && data.session.reason
  return jsxs('div', { style: { display: 'flex', flexDirection: 'column', gap: 10 }, children: [
    jsxs('div', { style: { display: 'flex', alignItems: 'center', gap: 8 }, children: [
      jsx('strong', { style: { flex: 1, fontSize: 14, color: c.primary }, children: t('title') }),
      jsx(TextButton, {
        disabled: loading, title: t('refreshTip'),
        onClick: () => load(true),
        children: [jsx(Icon, { name: 'RefreshCw', size: 12 }), loading ? t('refreshing') : t('refresh')]
      })
    ] }),
    account
      ? jsxs('div', {
        style: { display: 'flex', alignItems: 'center', gap: 8, padding: '8px 10px', borderRadius: 6, background: c.soft },
        children: [
          jsx(Swatch, { color: colorFor(account, colors), size: 10 }),
          jsxs('div', { style: { flex: 1, minWidth: 0, fontSize: 12, color: c.secondary, display: 'flex', flexDirection: 'column', gap: 2 }, children: [
            jsxs('span', { children: [
              kind === 'chat' ? t('chatUses') : t('newChatStarts'),
              jsx('strong', { style: { color: c.primary }, children: shortName(account) }),
              leftPct(account) !== null ? jsx('span', { style: { color: toneFor(leftPct(account)) }, children: t('leftPct', leftPct(account)) }) : null
            ] }),
            kind === 'chat' && reason
              ? jsx('span', { style: { fontSize: 10.5, color: c.tertiary }, children:
                reasonCode(reason) === 'manual' ? t('pickedByYou') : t('switchedOnItsOwn', reasonText(reason)) })
              : null,
            kind === 'chat' && best
              ? jsx('span', { style: { fontSize: 10.5, color: c.tertiary }, children:
                t('ifRunsOut', shortName(best), leftPct(best) !== null ? t('leftParen', leftPct(best)) : '') })
              : kind === 'chat' ? jsx('span', { style: { fontSize: 10.5, color: c.yellow }, children: t('noBackup') }) : null
          ] })
        ]
      })
      : null,
    jsx('div', { style: { fontSize: 11, color: c.tertiary, lineHeight: 1.45 }, children: t('intro') }),
    error ? jsx('div', { style: { fontSize: 11, color: c.red, overflowWrap: 'anywhere' }, children: error }) : null,
    dups ? jsx('div', { style: { fontSize: 11, color: c.yellow, lineHeight: 1.4 }, children:
      t('dups', real, data.accounts.length, dups) }) : null,
    jsx('div', { style: { maxHeight: 'min(520px, 64vh)', overflowY: 'auto', margin: '0 -6px', padding: '0 4px' }, children:
      data && data.accounts
        ? jsx(AccountList, { data, nowMs })
        : jsx('div', { style: { padding: '18px 0', fontSize: 12, color: c.secondary }, children: loading ? t('readingAccounts') : t('noDataYet') })
    }),
    jsx('div', { style: { fontSize: 10, color: c.quaternary }, children:
      (age !== null ? t('dataAge', ageText(age)) : '') + t('manualNote') })
  ] })
}

function Poller() {
  const open = useValue($open)
  const focused = useValue(host.state && host.state.focusedStoredSessionId ? host.state.focusedStoredSessionId : $now)
  useEffect(() => {
    if (open) load(false)
    const id = setInterval(() => load(false), open ? POLL_OPEN_MS : POLL_CLOSED_MS)
    return () => clearInterval(id)
  }, [open])
  useEffect(() => { load(false) }, [typeof focused === 'number' ? 0 : focused])
  return null
}

function Chip() {
  useValue($locale)
  const open = useValue($open)
  const data = useValue($data)
  const error = useValue($error)
  const nowMs = useValue($now)
  const { account, kind } = chatAccount(data)
  const colors = identityColors(data && data.accounts)
  const left = leftPct(account)
  const s = (account && account.status) || {}
  const blocked = s.state === 'limit' || s.state === 'paused'
  const name = account ? shortName(account) : 'Claude'
  const state = !account ? '' : blocked ? t('chipExhausted', whenText(s.until, nowMs)) : left === null ? '' : t('chipLeft', left)
  const five = account && account.five_hour ? Math.round(account.five_hour.used_pct) : null
  const week = account && account.seven_day ? Math.round(account.seven_day.used_pct) : null
  const title = t('title')
  const tip = account
    ? `${kind === 'chat' ? t('tipChatUses') : t('tipNewChat')}: ${name}${account.email ? ` (${account.email})` : ''}\n` +
      `${t('limit5h')}: ${five === null ? t('noData') : t('usedPct', five)}\n` +
      `${t('limitWeek')}: ${week === null ? t('noData') : t('usedPct', week)}\n` +
      t('tipClick')
    : error ? `${title}: ${error}` : title
  return jsxs(Popover, { open, onOpenChange: value => $open.set(value), children: [
    jsx(Poller, {}),
    jsx(PopoverTrigger, { asChild: true, children: jsxs('button', {
      type: 'button',
      'aria-label': title,
      title: tip,
      style: {
        display: 'inline-flex', alignItems: 'center', gap: 6, padding: '0 7px', height: '100%',
        color: c.tertiary, background: 'transparent', border: 'none', cursor: 'pointer',
        fontSize: '0.6875rem', fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap'
      },
      children: [
        jsx(Swatch, { color: account ? colorFor(account, colors) : c.quaternary, size: 8 }),
        jsx('span', { style: { color: c.secondary, fontWeight: 600, maxWidth: 140, overflow: 'hidden', textOverflow: 'ellipsis' }, children: name }),
        state ? jsx('span', { style: { color: blocked ? c.red : toneFor(left) }, children: state }) : null
      ]
    }) }),
    jsx(PopoverContent, {
      side: 'top', align: 'end', sideOffset: 8, 'aria-label': title,
      style: { width: 'min(460px, calc(100vw - 24px))', padding: 14, color: c.primary },
      children: jsx(Panel, {})
    })
  ] })
}

function setupI18n(ctx) {
  const i18n = ctx && ctx.i18n
  if (!i18n || typeof i18n.register !== 'function' || typeof i18n.t !== 'function') return
  try {
    i18n.register(MESSAGES)
    translate = (key, ...args) => i18n.t(key, ...args)
    if (typeof i18n.onLocaleChange === 'function') {
      i18n.onLocaleChange(() => $locale.set($locale.get() + 1))
    }
  } catch {
    translate = translateEnglish
  }
}

export default {
  id: PLUGIN_ID,
  name: 'Claude accounts',
  description: 'Claude account per chat, automatic switch to the one with most usage left when it runs out, drag to reorder, names and removing accounts.',
  defaultEnabled: true,
  register(ctx) {
    setupI18n(ctx)
    rest = ctx.rest
    load(false)
    const tick = setInterval(() => $now.set(Date.now()), 15000)
    if (typeof ctx.onDispose === 'function') {
      ctx.onDispose(() => {
        clearInterval(tick)
        rest = null
        translate = translateEnglish
      })
    }
    ctx.register({
      id: 'chip',
      area: (STATUSBAR_AREAS && STATUSBAR_AREAS.right) || 'statusBar.right',
      order: 98,
      render: () => jsx(Chip, {})
    })
  }
}
