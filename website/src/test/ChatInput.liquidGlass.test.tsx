/**
 * The composer sits inside ONE Liquid Glass dock pane (`composer-dock`, built
 * from components/Glass.tsx): `--glass-tint` over the blurred transcript, the
 * `--glass-band` light bands, `--glass-edge` side lines, no ring, and the neutral
 * `glass-shadow` for depth. The pane also holds an approval bar fused to the composer's top and the
 * collapsed bar, so those share the material instead of meeting it at a seam;
 * the wrapper's own surface and border are therefore transparent in every mode
 * (an incognito / temporary session still paints its coloured border). The pane
 * is always mounted: toggling it would remount the editor and drop the draft's
 * focus when an approval lands.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it, vi } from 'vitest'
vi.mock('@radix-ui/react-dropdown-menu', async () => await import('./__mocks__/@radix-ui/react-dropdown-menu'))
vi.mock('@radix-ui/react-popover', async () => await import('./__mocks__/@radix-ui/react-popover'))
import { screen } from '@testing-library/react'
import ChatInput from '../components/ChatInput'
import { createTestStore, renderWithProviders } from './helpers'
import type { RootState } from '../store'

const INDEX_CSS = readFileSync(resolve(process.cwd(), 'src/index.css'), 'utf-8')
const CHAT_INPUT_SRC = readFileSync(resolve(process.cwd(), 'src/components/ChatInput.tsx'), 'utf-8')

const dockOf = (wrapper: HTMLElement) => wrapper.closest('[data-testid="composer-dock"]') as HTMLElement

describe('composer liquid glass', () => {
  it('keeps the wrapper transparent so the dock pane shows through', () => {
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />)
    const wrapper = screen.getByTestId('input-wrapper')
    expect(wrapper.className).toContain('bg-transparent')
    expect(wrapper.className).toContain('border-transparent')
    expect(wrapper.className).not.toContain('bg-bg-elevated')
  })

  // With an approval box fused above, the bar and the composer share the ONE
  // dock pane: the wrapper stays transparent (no seam, no notch), keeps its
  // transparent border in every mode, and the dock swaps its shadow for the
  // approval glow so the pending decision is what lights up.
  it('keeps the wrapper on the shared pane and lights the approval glow while an approval is attached', () => {
    const store = createTestStore({
      chat: {
        activeSlot: 'slot-1',
        messages: [
          { role: 'user', content: 'list files' },
          {
            role: 'permission',
            content: 'Running: ls /tmp',
            meta: { approval_id: 'ap-1', request_id: 'req-1', tool_input: '{"command":"ls /tmp"}', tool_title: 'Running: ls /tmp', tool_call_id: 'tc-1' },
          },
        ],
        toolLog: [],
        slotStatusDetail: {},
      } as unknown as RootState['chat'],
      dashboard: {
        slots: [{ key: 'slot-1', messages: 2, running: true, pending_approval: true, waiting_for_input: false }],
        approvalMode: 'normal',
        connected: true,
        channelTrusted: false,
        refreshTrigger: 0,
        unreadSlots: [],
        updateProgress: null,
      } as unknown as RootState['dashboard'],
    })
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />, { store })
    const wrapper = screen.getByTestId('input-wrapper')
    expect(wrapper.className).toContain('bg-transparent')
    // No theme-colored focus cue on the composer: the glass IS the cue.
    expect(wrapper.className).not.toContain('focus-within:border-accent')
    expect(wrapper.className).not.toContain('bg-bg-elevated')
    const dock = dockOf(wrapper)
    expect(dock.className).toContain('approval-glow')
    // The glow rides on top of `glass-shadow`, so the textarea keeps the
    // material's tint + edge focus step while the decision is pending.
    expect(dock.className).toContain('glass-shadow')
    expect(screen.getByRole('button', { name: /allow once/i })).toBeTruthy()
  })

  it('mounts one dock pane around the wrapper: no ring on the box, neutral shadow, 16px glass, theme tint', () => {
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />)
    const wrapper = screen.getByTestId('input-wrapper')
    const dock = dockOf(wrapper)
    expect(dock).not.toBeNull()
    // The material draws no rim: the outer box carries only the caller's shadow.
    expect(dock.className).not.toMatch(/\bborder\b/)
    // Same neutral shadow as every other glass pane; no composer-only glow class.
    expect(dock.className).toContain('glass-shadow')
    expect(dock.style.borderRadius).toBe('16px')
    // Glass IS the host: the dock element itself is the LiquidGlass root (no
    // wrapper box), carrying the radius, the caller's class and the effect
    // layers, with the children rendered directly after them.
    expect(dock.classList.contains('liquid-glass')).toBe(true)
    expect(dock.style.isolation).toBe('isolate')
    // The tint rides the oversized frost box inside the clipping effect layer.
    const boxes = Array.from(dock.querySelectorAll<HTMLElement>(':scope > span[aria-hidden="true"] > span'))
    expect(boxes.some(l => l.style.background.includes('var(--glass-tint)'))).toBe(true)
    // Layers under the children: -1 inside the host's own stacking context.
    for (const layer of dock.querySelectorAll<HTMLElement>(':scope > span[aria-hidden="true"]')) expect(layer.style.zIndex).toBe('-1')
  })

  it('defines the glass tokens (tint, focus tint, band, edge, hairline, input inks) for both polarities', () => {
    // The dark tint is the reference material's over-black color rgb(54, 57, 62)
    // solved back into a translucent fill: a light, slightly blue tint, never a
    // black one (a black tint can only land BELOW the page). Smoked glass types
    // in pure white with a #9c9c9c placeholder; a light pane keeps the theme inks.
    expect(INDEX_CSS).toMatch(/:root \{ --glass-tint: rgba\(222, 234, 255, 0\.24\); --glass-tint-focus: rgba\(222, 234, 255, 0\.40\); --glass-band: rgba\(255, 255, 255, 0\.22\); --glass-edge: rgba\(255, 255, 255, 0\.35\); --glass-edge-focus: rgba\(255, 255, 255, 0\.70\); --glass-hairline: rgba\(0, 0, 0, 0\.50\); --glass-text: #ffffff; --glass-placeholder: #9c9c9c; \}/)
    expect(INDEX_CSS).toMatch(/\[data-mode="light"\] \{ --glass-tint: rgba\(238, 238, 243, 0\.45\); --glass-tint-focus: rgba\(255, 255, 255, 0\.92\); --glass-band: rgba\(255, 255, 255, 0\.92\); --glass-edge: rgba\(0, 0, 0, 0\.24\); --glass-edge-focus: rgba\(0, 0, 0, 0\.60\); --glass-hairline: rgba\(0, 0, 0, 0\.20\); --glass-text: var\(--text\); --glass-placeholder: var\(--muted\); \}/)
  })

  it('stands the context shelf on a short fade to page colour', () => {
    // The shelf (agent, project, branch, model chips) sits below the glass on
    // the bare transcript, which scrolls under the floating dock. It fades from
    // nothing at the pane's bottom edge to solid `--bg` by 60% of its height,
    // in its own stacking context so it paints above the transcript and below
    // the chips, spanning the pane width (the pane clips it) so no side edge
    // shows. The glass above it is untouched: no rule paints inside the pane.
    expect(INDEX_CSS).toMatch(/\.glass-shelf \{ position: relative; isolation: isolate; \}/)
    expect(INDEX_CSS).toMatch(/\.glass-shelf::before \{[^}]*top: 0; bottom: -4px; left: -100vw; right: -100vw;[^}]*linear-gradient\(to bottom, transparent, var\(--bg\) 60%\);[^}]*z-index: -1;/)
    expect(CHAT_INPUT_SRC).toMatch(/data-testid="composer-context-shelf" className="glass-shelf pt-1 flex items-center gap-2 min-w-0"/)
    expect(INDEX_CSS).not.toMatch(/glass-toolbar/)
  })

  it('brightens secondary ink inside a dark pane and cues clipped strips with a blur, not a colour', () => {
    // The retuned dark tint composites to ~rgb(67,71,81), where the theme's
    // --muted keeps barely 2:1; inside a pane every text-muted reads --glass-muted
    // (polarity-fixed: brighter on smoked glass, the theme's own on a light pane).
    expect(INDEX_CSS).toMatch(/:root \{ --glass-muted: #b4b4bc; \}/)
    expect(INDEX_CSS).toMatch(/\[data-mode="light"\] \{ --glass-muted: var\(--muted\); \}/)
    expect(INDEX_CSS).toMatch(/\.liquid-glass \{ --muted: var\(--glass-muted\); \}/)
    // The file-preview strip and the control row scroll on the glass, which has
    // no opaque colour to fade FROM: their edge cues are backdrop-blur wedges
    // masked toward the strip, never a from-bg-elevated gradient.
    expect(CHAT_INPUT_SRC).not.toMatch(/from-bg-elevated to-transparent/)
    expect(CHAT_INPUT_SRC.match(/backdrop-blur-\[3px\] \[mask-image:linear-gradient\(to_(right|left),black,transparent\)\]/g)?.length).toBe(4)
  })

  it('keeps a steady approval glow under reduced motion', () => {
    // The global reduced-motion rule runs every animation once for 0.01ms,
    // which parks the pulse at its 0% keyframe: --approval-shadow was then
    // transparent, so a pending approval showed no glow and the glass dock that
    // hands its shadow slot to it showed no shadow either.
    expect(INDEX_CSS).toMatch(/@media \(prefers-reduced-motion:reduce\)\{\.approval-glow\{animation:none;--glow-strength:\.6\}\}/)
  })

  // Every glass surface, the session composer included, wears the neutral
  // `glass-shadow`: focus is the deeper shadow PLUS the brighter focus tint and
  // darker side lines, never a theme-colored glow or ring. The tint step is for
  // neutral panes only: an accent / warn pane keeps its hue while a control
  // inside it has focus. `composer-halo` (the old accent focus glow) is gone.
  it('gives every glass pane the same neutral focus cue, no accent glow', () => {
    expect(INDEX_CSS).toMatch(/\.glass-shadow:focus-within \{ box-shadow: 0 0 18px rgba\(0, 0, 0, 0\.14\); \}/)
    // Focus steps the tint AND darkens the side lines (--glass-edge-focus); the
    // capsule's neutral focus cue is that pair, never an accent ring.
    expect(INDEX_CSS).toMatch(/\.glass-shadow:focus-within:not\(\.glass-accent, \.glass-warn\) \{ --glass-tint: var\(--glass-tint-focus\); --glass-edge: var\(--glass-edge-focus\); \}/)
    expect(INDEX_CSS).not.toContain('composer-halo')
    // Base-layer `.glass-shadow` rules (` {` spaced) carry no theme color; the
    // solidifying fallbacks (`{` unspaced) are the one place an accent outline is allowed.
    expect(INDEX_CSS).not.toMatch(/\.glass-shadow[^{]* \{[^}]*--accent/)
  })

  // There is ONE material: every dock surface is the primitive rendered as its
  // own element. The only per-call-site CSS is which tint step a pane is on,
  // and each step is a `--glass-tint` swap derived once on :root.
  it('has no CSS copy of the material, only tint steps on the host', () => {
    expect(INDEX_CSS).not.toContain('glass-pane')
    expect(INDEX_CSS).toMatch(/:root \{ --glass-tint-accent: color-mix\(in srgb, var\(--accent\) 14%, var\(--glass-tint\)\); --glass-tint-warn: color-mix\(in srgb, var\(--warn\) 12%, var\(--glass-tint\)\); --glass-tint-hover: color-mix\(in srgb, var\(--text\) 8%, var\(--glass-tint\)\); \}/)
    expect(INDEX_CSS).toContain('.glass-accent { --glass-tint: var(--glass-tint-accent); }')
    expect(INDEX_CSS).toContain('.glass-warn { --glass-tint: var(--glass-tint-warn); }')
    expect(INDEX_CSS).toContain('.glass-hover:hover { --glass-tint: var(--glass-tint-hover); }')
  })

  // The material must solidify wherever the app's other glass does: reduced
  // transparency, increased contrast, and a Chromium built without
  // backdrop-filter (#1817) — otherwise the transcript would show through the
  // box the user is typing into and through every chip above it.
  it('solidifies the pane under every glass fallback rule', () => {
    for (const block of [/@supports not \(\(backdrop-filter[\s\S]*?\n\}/, /@media \(prefers-reduced-transparency: reduce\)\{[\s\S]*?\n\}/, /@media \(prefers-contrast: more\)\{[\s\S]*?\n\}/]) {
      const rule = INDEX_CSS.match(block)?.[0] ?? ''
      expect(rule, String(block)).toContain('.liquid-glass{ background:var(--bg-elevated) !important')
      // The hide rule names the primitive's own layer attribute, never
      // `aria-hidden`: the children render directly, so a decorative icon
      // (`<Lightbulb aria-hidden>` in TipCard) is a direct child too and an
      // `aria-hidden` selector would delete it with the layers.
      expect(rule, String(block)).toContain('.liquid-glass>[data-liquid-glass-layer]{ display:none !important }')
      expect(rule, String(block)).not.toContain('[aria-hidden="true"]{ display:none')
      // The neutral pane's focus cue (tint + side-line step) lives in those
      // hidden layers, so a solidified pane holding focus needs the standard ring.
      expect(rule, String(block)).toMatch(/\.glass-shadow:focus-within\{ outline:2px solid var\(--accent\) !important; outline-offset:2px/)
    }
  })

  // The solid fallback fill is !important, so the picked chip and the incognito
  // chip must re-assert their hue on it or lose their only visible difference.
  it('keeps the accent and warn tints under the solidifying fallbacks', () => {
    for (const block of [/@supports not \(\(backdrop-filter[\s\S]*?\n\}/, /@media \(prefers-reduced-transparency: reduce\)\{[\s\S]*?\n\}/]) {
      const rule = INDEX_CSS.match(block)?.[0] ?? ''
      expect(rule, String(block)).toContain('.liquid-glass.glass-accent{ background:color-mix(in srgb, var(--accent) 14%, var(--bg-elevated)) !important }')
      expect(rule, String(block)).toContain('.liquid-glass.glass-warn{ background:color-mix(in srgb, var(--warn) 12%, var(--bg-elevated)) !important }')
    }
  })

  // `.glass-hover` steps `--glass-tint`, which only the hidden frost layer reads,
  // so each solidifying block moves the hover feedback onto the solid fill. The
  // contrast block doubles the mix: more-contrast users get the clearest cue.
  it('keeps hover feedback on glass buttons under all three fallbacks', () => {
    for (const [block, mix] of [[/@supports not \(\(backdrop-filter[\s\S]*?\n\}/, '8%'], [/@media \(prefers-reduced-transparency: reduce\)\{[\s\S]*?\n\}/, '8%'], [/@media \(prefers-contrast: more\)\{[\s\S]*?\n\}/, '16%']] as const) {
      const rule = INDEX_CSS.match(block)?.[0] ?? ''
      expect(rule, String(block)).toContain(`.liquid-glass.glass-hover:hover{ background:color-mix(in srgb, var(--text) ${mix}, var(--bg-elevated)) !important }`)
    }
  })
})
