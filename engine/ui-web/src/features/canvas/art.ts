/* The canvas's pictures, drawn here rather than shipped as files: the cartoon avatars an
 * employee can wear, the office backgrounds a company or a department can have, and the
 * letter a company shows until it is given a logo. All of them are data URLs or CSS values,
 * so nothing is fetched and nothing new has to be packaged. */

export interface Avatar {
  id: string
  /** A `data:image/svg+xml` URL, ready for an <img>. */
  src: string
}

export interface Backdrop {
  id: string
  /** The catalogue key of its name. */
  key: string
  /** A CSS `background` value. */
  css: string
}

const svgUrl = (body: string): string =>
  `data:image/svg+xml;utf8,${encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">${body}</svg>`)}`

/* -- avatars ----------------------------------------------------------------------- */

interface Face {
  bg: string
  skin: string
  hair: string
  style: 'short' | 'long' | 'bun' | 'spiky' | 'bald' | 'curly'
  extra?: 'glasses' | 'headset' | 'bow' | 'beard' | 'cap'
}

const HAIR: Record<Face['style'], (c: string) => string> = {
  short: (c) => `<path d="M17 27c0-10 7-16 15-16s15 6 15 16c-3-5-8-8-15-8s-12 3-15 8z" fill="${c}"/>`,
  long: (c) => `<path d="M15 44V28c0-10 8-17 17-17s17 7 17 17v16c-3-2-4-6-4-12-4-4-8-6-13-6s-9 2-13 6c0 6-1 10-4 12z" fill="${c}"/>`,
  bun: (c) => `<circle cx="32" cy="10" r="6" fill="${c}"/><path d="M17 27c0-9 7-15 15-15s15 6 15 15c-4-4-9-6-15-6s-11 2-15 6z" fill="${c}"/>`,
  spiky: (c) => `<path d="M16 28l3-12 5 6 4-10 4 9 4-10 4 10 5-6 3 13c-4-5-9-8-16-8s-12 3-16 8z" fill="${c}"/>`,
  bald: (c) => `<path d="M18 30c0-3 1-5 2-6M46 30c0-3-1-5-2-6" stroke="${c}" stroke-width="3" stroke-linecap="round" fill="none"/>`,
  curly: (c) => `<g fill="${c}"><circle cx="20" cy="24" r="6"/><circle cx="27" cy="17" r="7"/><circle cx="37" cy="17" r="7"/><circle cx="44" cy="24" r="6"/></g>`,
}

const EXTRA: Record<NonNullable<Face['extra']>, string> = {
  glasses: '<g fill="none" stroke="#1d1d1d" stroke-width="2"><circle cx="26" cy="33" r="5"/><circle cx="38" cy="33" r="5"/><path d="M31 33h2"/></g>',
  headset: '<path d="M16 34c0-11 7-19 16-19s16 8 16 19" fill="none" stroke="#2b2b2b" stroke-width="3"/><rect x="13" y="31" width="6" height="10" rx="3" fill="#2b2b2b"/><rect x="45" y="31" width="6" height="10" rx="3" fill="#2b2b2b"/><path d="M48 41c0 5-4 8-10 8" fill="none" stroke="#2b2b2b" stroke-width="2"/>',
  bow: '<path d="M38 14l8-5v10zM38 14l-8-5v10z" fill="#e2507a"/><circle cx="38" cy="14" r="2.5" fill="#c43865"/>',
  beard: '<path d="M21 38c2 9 6 13 11 13s9-4 11-13c-3 3-7 4-11 4s-8-1-11-4z" fill="#5a3b24"/>',
  cap: '<path d="M15 27c0-9 8-15 17-15s17 6 17 15z" fill="#2f6fd6"/><path d="M40 26h14c0 2-2 3-4 3H40z" fill="#245bb3"/>',
}

function person(f: Face): string {
  return svgUrl(
    `<circle cx="32" cy="32" r="32" fill="${f.bg}"/>`
    + `<path d="M14 64c2-11 9-16 18-16s16 5 18 16z" fill="${f.hair === '#f3f3f3' ? '#7b8794' : '#3d4a5c'}"/>`
    + `<rect x="28" y="40" width="8" height="9" rx="3" fill="${f.skin}"/>`
    + `<ellipse cx="32" cy="31" rx="14" ry="15" fill="${f.skin}"/>`
    + HAIR[f.style](f.hair)
    + '<circle cx="26" cy="33" r="2" fill="#1d1d1d"/><circle cx="38" cy="33" r="2" fill="#1d1d1d"/>'
    + '<path d="M27 40c3 3 7 3 10 0" fill="none" stroke="#1d1d1d" stroke-width="2" stroke-linecap="round"/>'
    + '<circle cx="22" cy="38" r="2.4" fill="#f08a7a" opacity=".45"/><circle cx="42" cy="38" r="2.4" fill="#f08a7a" opacity=".45"/>'
    + (f.extra ? EXTRA[f.extra] : ''),
  )
}

function robot(bg: string, body: string, eye: string): string {
  return svgUrl(
    `<circle cx="32" cy="32" r="32" fill="${bg}"/>`
    + `<path d="M32 9v6" stroke="${body}" stroke-width="3"/><circle cx="32" cy="8" r="3" fill="${eye}"/>`
    + `<rect x="15" y="15" width="34" height="28" rx="9" fill="${body}"/>`
    + `<rect x="20" y="22" width="24" height="12" rx="6" fill="#1d2433"/>`
    + `<circle cx="27" cy="28" r="3" fill="${eye}"/><circle cx="37" cy="28" r="3" fill="${eye}"/>`
    + `<rect x="11" y="24" width="4" height="10" rx="2" fill="${body}"/><rect x="49" y="24" width="4" height="10" rx="2" fill="${body}"/>`
    + `<path d="M18 64c1-10 6-15 14-15s13 5 14 15z" fill="${body}" opacity=".85"/>`,
  )
}

function animal(bg: string, fur: string, ear: 'cat' | 'bear' | 'fox', muzzle: string): string {
  const ears = ear === 'cat' || ear === 'fox'
    ? `<path d="M17 22l3-13 10 9zM47 22l-3-13-10 9z" fill="${fur}"/>`
    : `<circle cx="19" cy="17" r="7" fill="${fur}"/><circle cx="45" cy="17" r="7" fill="${fur}"/>`
  return svgUrl(
    `<circle cx="32" cy="32" r="32" fill="${bg}"/>${ears}`
    + `<ellipse cx="32" cy="33" rx="16" ry="15" fill="${fur}"/>`
    + `<ellipse cx="32" cy="40" rx="8" ry="6" fill="${muzzle}"/>`
    + '<circle cx="25" cy="31" r="2.2" fill="#1d1d1d"/><circle cx="39" cy="31" r="2.2" fill="#1d1d1d"/>'
    + '<path d="M30 38h4l-2 2.5z" fill="#1d1d1d"/>'
    + `<path d="M18 64c2-9 7-13 14-13s12 4 14 13z" fill="${fur}" opacity=".8"/>`,
  )
}

const FACES: Face[] = [
  { bg: '#f6c86b', skin: '#f2c9a5', hair: '#3b2a20', style: 'short' },
  { bg: '#9cd3f5', skin: '#e8b48c', hair: '#1f1a17', style: 'long', extra: 'bow' },
  { bg: '#b7e3a8', skin: '#c68c5e', hair: '#151515', style: 'curly' },
  { bg: '#f5a3a3', skin: '#f6d2b6', hair: '#c96a2d', style: 'bun', extra: 'glasses' },
  { bg: '#c9b6f2', skin: '#8d5a3b', hair: '#121212', style: 'spiky', extra: 'headset' },
  { bg: '#ffd9a3', skin: '#f1c7a2', hair: '#e1b04a', style: 'long' },
  { bg: '#a8e6df', skin: '#d9a07a', hair: '#4a3324', style: 'short', extra: 'beard' },
  { bg: '#f7b9d7', skin: '#f4cdb0', hair: '#2a1d16', style: 'bun' },
  { bg: '#b3c7f7', skin: '#e3b18a', hair: '#f3f3f3', style: 'bald', extra: 'glasses' },
  { bg: '#ffe08a', skin: '#a8714c', hair: '#1b1b1b', style: 'short', extra: 'cap' },
  { bg: '#d1f0b1', skin: '#f2c9a5', hair: '#7a4b2a', style: 'curly', extra: 'headset' },
  { bg: '#f2c2a0', skin: '#e9b995', hair: '#2b2b2b', style: 'spiky' },
]

export const AVATARS: readonly Avatar[] = [
  ...FACES.map((f, i) => ({ id: `p${i + 1}`, src: person(f) })),
  { id: 'r1', src: robot('#bfe3ff', '#7d93ad', '#56e0ff') },
  { id: 'r2', src: robot('#ffd8b0', '#c9884d', '#ffe36e') },
  { id: 'r3', src: robot('#d7f5d0', '#5f9e6e', '#b6ff8f') },
  { id: 'r4', src: robot('#e6d6ff', '#7f6bb3', '#ff8fd2') },
  { id: 'a1', src: animal('#ffe2b8', '#e98a3a', 'fox', '#fff1e0') },
  { id: 'a2', src: animal('#d9ecff', '#8b8f99', 'cat', '#eef0f3') },
  { id: 'a3', src: animal('#e9f7d4', '#8a5a3b', 'bear', '#d8b593') },
  { id: 'a4', src: animal('#ffd6e3', '#f2f2f2', 'cat', '#ffffff') },
]

export const avatarSrc = (id: string): string | null => AVATARS.find((a) => a.id === id)?.src ?? null

/* -- backgrounds -------------------------------------------------------------------- */

export const BACKDROPS: readonly Backdrop[] = [
  {
    id: 'carpet', key: 'gui.canvas.bg_carpet',
    css: 'radial-gradient(rgba(255,255,255,.05) 1px, transparent 1.6px) 0 0/14px 14px, linear-gradient(160deg, #232831, #1b1f26)',
  },
  {
    id: 'wood', key: 'gui.canvas.bg_wood',
    css: 'repeating-linear-gradient(90deg, rgba(0,0,0,.18) 0 2px, transparent 2px 46px), '
      + 'repeating-linear-gradient(0deg, rgba(255,255,255,.03) 0 1px, transparent 1px 9px), linear-gradient(160deg, #3a2a1c, #2b1f15)',
  },
  {
    id: 'glass', key: 'gui.canvas.bg_glass',
    css: 'linear-gradient(120deg, rgba(150,200,255,.14), rgba(150,200,255,.02) 35%, rgba(255,255,255,.07) 55%, rgba(150,200,255,.03)), '
      + 'linear-gradient(160deg, #1d2633, #161d27)',
  },
  {
    id: 'blueprint', key: 'gui.canvas.bg_blueprint',
    css: 'linear-gradient(rgba(140,180,255,.13) 1px, transparent 1px) 0 0/22px 22px, '
      + 'linear-gradient(90deg, rgba(140,180,255,.13) 1px, transparent 1px) 0 0/22px 22px, linear-gradient(160deg, #15284a, #10203b)',
  },
  {
    id: 'garden', key: 'gui.canvas.bg_garden',
    css: 'radial-gradient(circle at 18% 28%, rgba(110,180,100,.28), transparent 38%), '
      + 'radial-gradient(circle at 82% 72%, rgba(70,150,90,.24), transparent 44%), linear-gradient(160deg, #1b2a1e, #142017)',
  },
  {
    id: 'brick', key: 'gui.canvas.bg_brick',
    css: 'linear-gradient(rgba(0,0,0,.28) 2px, transparent 2px) 0 0/52px 24px, '
      + 'linear-gradient(90deg, rgba(0,0,0,.28) 2px, transparent 2px) 0 0/52px 48px, '
      + 'linear-gradient(90deg, rgba(0,0,0,.28) 2px, transparent 2px) 26px 24px/52px 48px, linear-gradient(160deg, #46291f, #361f18)',
  },
  {
    id: 'night', key: 'gui.canvas.bg_night',
    css: 'radial-gradient(rgba(255,214,120,.55) 1px, transparent 1.8px) 0 0/36px 28px, linear-gradient(170deg, #182036, #0f1424)',
  },
  {
    id: 'sunny', key: 'gui.canvas.bg_sunny',
    css: 'radial-gradient(circle at 85% 10%, rgba(255,200,110,.30), transparent 45%), linear-gradient(160deg, #3a3020, #2a2318)',
  },
]

export const DEFAULT_COMPANY_BG = 'carpet'
export const DEFAULT_DEPT_BG = 'wood'

export const backdropCss = (id: string, fallback: string): string =>
  (BACKDROPS.find((b) => b.id === id) ?? BACKDROPS.find((b) => b.id === fallback) ?? BACKDROPS[0]!).css

/* -- the company letter ------------------------------------------------------------- */

const TINTS = ['#e8913f', '#4f8ef0', '#3fb27f', '#c065e0', '#e05a6d', '#d4a10e', '#2bb3c0', '#8d7cf0']

/** A rounded tile with the first letter of the name: what a company shows until it has a logo. */
export function letterLogo(name: string): string {
  const letter = (name.trim()[0] ?? '?').toUpperCase()
  let h = 0
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0
  const tint = TINTS[h % TINTS.length]
  const safe = letter.replace(/[<>&"']/g, '?')
  return svgUrl(
    `<rect width="64" height="64" rx="16" fill="${tint}"/>`
    + `<text x="32" y="43" text-anchor="middle" font-family="Segoe UI, Arial, sans-serif" font-size="32" font-weight="700" fill="#fff">${safe}</text>`,
  )
}
