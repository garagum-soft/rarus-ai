import { describe, expect, it } from 'vitest'

import { AVATARS, BACKDROPS, avatarSrc, backdropCss, letterLogo } from './art'
import { CARD_H, CARD_W } from './graph'
import {
  boxAt, canTalk, deptKey, dropCompany, dropDepartment, exportCompany, layoutCompanies, newCompanyId, normalize, placeMember, renameDepartment,
  talksWith,
} from './org'

import type { OrgRow } from './graph'
import type { Company } from './org'

const m = (name: string, company = '', department = ''): OrgRow => ({ name, reports_to: null, department, title: '', company, avatar: '' })

const acme = (over: Partial<Company> = {}): Company => ({
  id: 'acme', name: 'Acme', logo: '', head: 'ceo', frozen: false, background: '',
  departments: [{ name: 'Dev', head: 'devlead', background: '' }, { name: 'QA', head: '', background: '' }],
  ...over,
})

const org = [m('ceo', 'acme'), m('pm', 'acme'), m('devlead', 'acme', 'Dev'), m('dev', 'acme', 'Dev'), m('qa', 'acme', 'QA'), m('free')]

describe('who reports to whom inside a company', () => {
  it('follows the boxes: department members to their head, the rest to the company head', () => {
    const lines = Object.fromEntries(normalize(org, [acme()]).map((r) => [r.name, r.reports_to]))
    expect(lines).toEqual({ ceo: null, pm: 'ceo', devlead: 'ceo', dev: 'devlead', qa: 'ceo', free: null })
  })

  it('leaves the free cards as they were', () => {
    const rows = [{ ...m('a'), reports_to: 'b' }, m('b')]
    expect(normalize(rows, [])[0]?.reports_to).toBe('b')
  })
})

describe('who talks to whom', () => {
  it('is the engine rule: a department inside itself, heads and the company level across', () => {
    const c = [acme()]
    expect(canTalk(org, c, 'dev', 'devlead')).toBe(true)
    expect(canTalk(org, c, 'pm', 'devlead')).toBe(true)
    expect(canTalk(org, c, 'ceo', 'pm')).toBe(true)
    expect(canTalk(org, c, 'pm', 'dev')).toBe(false)
    expect(canTalk(org, c, 'dev', 'qa')).toBe(false)
    expect(canTalk(org, c, 'free', 'ceo')).toBe(false)
    expect(talksWith(org, c, 'devlead')).toEqual(['ceo', 'dev', 'pm'])
  })
})

describe('moving people and boxes', () => {
  it('moves a member and takes the head post away when it leaves the department', () => {
    const out = placeMember(org, [acme()], 'devlead', 'acme', 'QA')
    expect(out.org.find((r) => r.name === 'devlead')?.department).toBe('QA')
    expect(out.companies[0]?.departments.find((d) => d.name === 'Dev')?.head).toBe('')
  })

  it('takes a member out of every company', () => {
    const out = placeMember(org, [acme()], 'ceo', '', '')
    expect(out.org.find((r) => r.name === 'ceo')).toMatchObject({ company: '', department: '', reports_to: null })
    expect(out.companies[0]?.head).toBe('')
  })

  it('adds a row for a card that had none', () => {
    const out = placeMember([], [acme()], 'new', 'acme', 'Dev')
    expect(out.org).toEqual([{ name: 'new', reports_to: null, department: 'Dev', title: '', company: 'acme', avatar: '' }])
  })

  it('drops a company to the canvas and a department to the company level', () => {
    expect(dropCompany(org, [acme()], 'acme').org.filter((r) => r.company)).toEqual([])
    const out = dropDepartment(org, [acme()], 'acme', 'Dev')
    expect(out.org.find((r) => r.name === 'dev')?.department).toBe('')
    expect(out.companies[0]?.departments.map((d) => d.name)).toEqual(['QA'])
  })

  it('renames a department with its people', () => {
    const out = renameDepartment(org, [acme()], 'acme', 'Dev', 'Engineering')
    expect(out.org.find((r) => r.name === 'dev')?.department).toBe('Engineering')
    expect(out.companies[0]?.departments[0]?.name).toBe('Engineering')
  })

  it('makes readable ids that are not taken', () => {
    expect(newCompanyId([], 'Garagum Games')).toBe('garagum-games')
    expect(newCompanyId([acme({ id: 'acme' })], 'ACME')).toBe('acme-2')
    expect(newCompanyId([], 'Рога и копыта')).toBe('company')
  })
})

describe('the layout', () => {
  it('puts departments inside their company and cards inside their department', () => {
    const boxes = layoutCompanies(org, [acme()], {}, {}, 1000)
    const co = boxes.companies.get('acme')!
    const dev = boxes.depts.get(deptKey('acme', 'Dev'))!
    expect(co.x).toBe(1000)
    expect(dev.x).toBeGreaterThanOrEqual(co.x)
    expect(dev.y + dev.h).toBeLessThanOrEqual(co.y + co.h)
    const p = boxes.cards.get('dev')!
    expect(p.x).toBeGreaterThanOrEqual(dev.x)
    expect(p.x + CARD_W).toBeLessThanOrEqual(dev.x + dev.w)
    expect(p.y + CARD_H).toBeLessThanOrEqual(dev.y + dev.h)
    /* The head of the department takes the first place. */
    expect(boxes.cards.get('devlead')!.x).toBeLessThan(p.x)
    expect(boxAt(boxes, p.x + 5, p.y + 5)).toEqual({ company: 'acme', dept: 'Dev' })
    expect(boxAt(boxes, co.x + 5, co.y + 5)).toEqual({ company: 'acme', dept: '' })
    expect(boxAt(boxes, co.x - 50, co.y)).toBeNull()
  })

  it('grows a box around a card dragged to its edge, and keeps a dragged box where it was put', () => {
    const plain = layoutCompanies(org, [acme()], {}, {}, 0)
    const dev = plain.depts.get(deptKey('acme', 'Dev'))!
    const far = { x: dev.x + 900, y: dev.y + 700 }
    const grown = layoutCompanies(org, [acme()], { dev: far }, { 'c:acme': { x: 0, y: 0 } }, 0)
    expect(grown.depts.get(deptKey('acme', 'Dev'))!.w).toBeGreaterThan(dev.w)
    expect(grown.companies.get('acme')!.h).toBeGreaterThan(plain.companies.get('acme')!.h)
    const moved = layoutCompanies(org, [acme()], {}, { 'c:acme': { x: 500, y: 300 } }, 0)
    expect(moved.companies.get('acme')).toMatchObject({ x: 500, y: 300 })
  })
})

describe('export', () => {
  it('writes a company with its people', () => {
    const file = JSON.parse(exportCompany(org, [acme()], 'acme'))
    expect(file.kind).toBe('company')
    expect(file.members.map((r: OrgRow) => r.name).sort()).toEqual(['ceo', 'dev', 'devlead', 'pm', 'qa'])
  })
})

describe('the pictures', () => {
  it('has an avatar set and office backgrounds, all drawn in place', () => {
    expect(AVATARS.length).toBeGreaterThanOrEqual(16)
    for (const a of AVATARS) expect(a.src.startsWith('data:image/svg+xml')).toBe(true)
    expect(avatarSrc('nope')).toBeNull()
    expect(BACKDROPS.length).toBeGreaterThanOrEqual(6)
    expect(backdropCss('missing', 'wood')).toBe(BACKDROPS.find((b) => b.id === 'wood')?.css)
  })

  it('draws a company without a logo as its first letter', () => {
    expect(decodeURIComponent(letterLogo('acme'))).toContain('>A</text>')
    expect(decodeURIComponent(letterLogo('<script>'))).not.toContain('<script>')
  })
})
