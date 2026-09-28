// Evidence capture: the composer while an automatic compaction holds the session,
// and the transcript card a declined Stop settles into.
//
// Two shots against the REAL built SPA (website/dist) with the dashboard API
// stubbed. Not a test: the vitest files pin the behaviour; this shows it.
//
//   node scripts/capture-compacting-stop.mjs [outDir]
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/compacting-stop'
mkdirSync(OUT, { recursive: true })

const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const manual = JSON.parse(readFileSync(LOCALES + 'en.manual.json', 'utf-8'))
const HINT = manual.components.chatInput.compacting_context
const CARD = manual.pages.chat.stopEventCard.stop_declined_compacting_2
if (!HINT || !CARD) throw new Error('compacting keys missing from en.manual.json')

const SLOT = 'compacting-demo'
const now = Math.floor(Date.now() / 1000)

// The gateway writes the envelope into `cls`, mirrors it into `content`, and
// serves it parsed as `meta` (dashboard/state.py parse_cls_meta). All three, as
// the HTTP history endpoint would.
const stopMeta = {
  kind: 'stop_event',
  id: 'stop-demo',
  state: 'stop_declined_compacting',
  outcome: 'compacting',
  ts_start: new Date((now - 5) * 1000).toISOString(),
  ts_end: new Date((now - 4) * 1000).toISOString(),
}
const stopCard = JSON.stringify(stopMeta)

const slots = [{
  key: SLOT,
  title: 'Long investigation session',
  running: false,
  compacting: true,
  stop_state: 'idle',
  last_message: 'Cross-referenced the last three incident reports.',
  messages: 3,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  modified: now,
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 3,
  queue: [],
  messages: [
    { role: 'user', ts: now - 900, content: 'Keep going through the incident reports and summarise each one.' },
    { role: 'assistant', ts: now - 60, content: 'Cross-referenced the last three incident reports. Two share a root cause in the retry path.' },
    { role: 'system', ts: now - 5, content: stopCard, cls: stopCard, meta: stopMeta },
  ],
  context_pct: 87,
  context_used_tokens: 174_000,
  context_window_tokens: 200_000,
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    slots,
    extra: async (path, route) => {
      if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
      return false
    },
  })
  await page.goto(base + `/chat?sid=${encodeURIComponent(SLOT)}`, { waitUntil: 'domcontentloaded' })
  await page.getByTestId('compacting-hint').waitFor({ timeout: 15000 })
  await page.getByText(HINT, { exact: true }).waitFor({ timeout: 5000 })
  await page.getByText(CARD, { exact: true }).waitFor({ timeout: 5000 })
  await page.waitForTimeout(300)
  await page.screenshot({ path: `${OUT}/compacting-composer.png` })
  const composer = page.getByTestId('compacting-indicator')
  await composer.screenshot({ path: `${OUT}/compacting-indicator.png` })
  const card = page.getByTestId('stop-event-card')
  await card.screenshot({ path: `${OUT}/stop-declined-card.png` })
  await browser.close()
  srv.close()
  console.log(`wrote ${OUT}/compacting-composer.png, compacting-indicator.png, stop-declined-card.png`)
}

main().catch(err => { console.error(err); process.exit(1) })
