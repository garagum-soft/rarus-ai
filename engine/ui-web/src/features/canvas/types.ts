/* The agent canvas's contracts: what its source speaks to the gateway. */

import type { AgentNode, OrgRow } from './graph'
import type { Company } from './org'

export interface CanvasData {
  /* One card per name: employees, roster agents and names only the chart holds. */
  agents: AgentNode[]
  org: OrgRow[]
  companies: Company[]
  /* The deepest level the engine accepts (raven/agent/subagent/org_store.py). */
  maxDepth: number
}

export interface Saved {
  org: OrgRow[]
  companies: Company[]
}

/** What the canvas asks for when it hires an employee into a company or a department. */
export interface HireSpec {
  name: string
  role: string
  system_prompt: string
}

export interface CanvasSource {
  load(): Promise<CanvasData>
  /* Replaces the whole chart and the companies. The engine refuses one that is not a forest or
     whose memberships do not add up, and answers with what it wrote. */
  saveOrg(rows: OrgRow[], companies: Company[]): Promise<Saved>
  /* An employee's system prompt; only employees have one the page can edit. */
  promptGet(name: string): Promise<string>
  promptSet(name: string, prompt: string): Promise<boolean>
  /* A new employee (an agent of its own, raven/agent/subagent/hire.py); answers its card. */
  hire(spec: HireSpec): Promise<AgentNode>
  /* Lets an employee go: its folder is kept aside by the engine, the card leaves the canvas. */
  retire(name: string): Promise<void>
}
