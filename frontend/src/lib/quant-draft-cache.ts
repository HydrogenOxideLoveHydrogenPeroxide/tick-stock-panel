import type { QuantPortfolio } from './api'
import { portfolioToDraft, quantDraftDirty, type PortfolioDraft } from './quant-assistant'

export const QUANT_DRAFT_KEY = 'quant-assistant-draft-v1'
type DraftStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'> | undefined
export type DraftRecovery = { status: 'empty' | 'restored' | 'conflict' | 'unavailable' | 'invalid'; draft?: PortfolioDraft }

function validDraft(value: unknown): value is PortfolioDraft {
  if (!value || typeof value !== 'object') return false
  const draft = value as PortfolioDraft
  const template = portfolioToDraft({ cash: 0, holdings: [], strategy: {
    kind: 'trend', screener_strategy_id: '', stop_loss_pct: 8, take_profit_pct: 20,
    max_position_pct: 25, target_position_pct: 15, cash_reserve_pct: 10,
  } })
  const strings = (record: unknown, keys: string[]) => !!record && typeof record === 'object' &&
    Object.keys(record).length === keys.length && keys.every(key => typeof (record as Record<string, unknown>)[key] === 'string' && ((record as Record<string, string>)[key]).length <= 1000)
  return typeof draft.cash === 'string' && draft.cash.length <= 128 && Array.isArray(draft.holdings) && draft.holdings.length <= 20 &&
    draft.holdings.every(h => strings(h, ['symbol', 'name', 'quantity', 'available_quantity', 'cost_price', 'lot_size', 'note'])) &&
    strings(draft.strategy, Object.keys(template.strategy))
}

export function retainQuantDraft(storage: DraftStorage, draft: PortfolioDraft, saved: QuantPortfolio): boolean {
  try {
    if (!storage) return false
    if (!quantDraftDirty(draft, saved)) storage.removeItem(QUANT_DRAFT_KEY)
    else {
      if (!validDraft(draft)) return false
      storage.setItem(QUANT_DRAFT_KEY, JSON.stringify({ version: 1, base: saved, draft }))
    }
    return true
  } catch { return false }
}

export function recoverQuantDraft(storage: DraftStorage, saved: QuantPortfolio): DraftRecovery {
  try {
    if (!storage) return { status: 'unavailable' }
    const text = storage.getItem(QUANT_DRAFT_KEY)
    if (!text) return { status: 'empty' }
    if (text.length > 128 * 1024) return { status: 'invalid' }
    const value = JSON.parse(text)
    if (value.version !== 1 || !validDraft(value.draft)) return { status: 'invalid' }
    // A save may finish after navigation; don't mistake the committed draft for a conflict.
    if (!quantDraftDirty(value.draft, saved)) { storage.removeItem(QUANT_DRAFT_KEY); return { status: 'empty' } }
    const sameBase = JSON.stringify(portfolioToDraft(value.base)) === JSON.stringify(portfolioToDraft(saved))
    return { status: sameBase ? 'restored' : 'conflict', draft: value.draft }
  } catch { return { status: 'unavailable' } }
}

export function clearQuantDraft(storage: DraftStorage): boolean {
  try { if (!storage) return false; storage.removeItem(QUANT_DRAFT_KEY); return true } catch { return false }
}
