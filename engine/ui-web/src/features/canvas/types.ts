/* The agent canvas's contracts: what its source speaks to the gateway. */

import type { AgentNode, OrgRow } from './graph'

export interface CanvasData {
  /* One card per name: employees, roster agents and names only the chart holds. */
  agents: AgentNode[]
  org: OrgRow[]
  /* The deepest level the engine accepts (raven/config/update_org.py). */
  maxDepth: number
}

export interface CanvasSource {
  load(): Promise<CanvasData>
  /* Replaces the whole chart. The engine refuses one that is not a forest, and answers with
     the rows as it wrote them. */
  saveOrg(rows: OrgRow[]): Promise<OrgRow[]>
  /* An employee's system prompt; only employees have one the page can edit. */
  promptGet(name: string): Promise<string>
  promptSet(name: string, prompt: string): Promise<boolean>
}
