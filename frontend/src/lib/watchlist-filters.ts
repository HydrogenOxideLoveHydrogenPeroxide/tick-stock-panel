const LEGACY_BOARDS = ['沪主板', '深主板', '创业板', '科创板', '北交所'] as const
export const WATCHLIST_BOARDS = [...LEGACY_BOARDS, 'ETF/基金', '其他'] as const
export type WatchlistBoard = typeof WATCHLIST_BOARDS[number]

export function getWatchlistBoard(symbol: string): WatchlistBoard {
  if (/^(5\d{5}|1[56]\d{4})(\.|$)/.test(symbol)) return 'ETF/基金'
  if (/^(300|301)/.test(symbol)) return '创业板'
  if (/^688/.test(symbol)) return '科创板'
  if (/\.BJ$/.test(symbol)) return '北交所'
  if (/^60[0135]/.test(symbol)) return '沪主板'
  if (/^00[012]/.test(symbol)) return '深主板'
  return '其他'
}

export function normalizeWatchlistBoards(saved: string[]): Set<string> {
  // 旧版的“全部股票板块”仍表示全部，不应在升级后意外隐藏基金。
  if (saved.length === LEGACY_BOARDS.length && LEGACY_BOARDS.every(board => saved.includes(board))) {
    return new Set(WATCHLIST_BOARDS)
  }
  return new Set(saved.filter(board => WATCHLIST_BOARDS.includes(board as WatchlistBoard)))
}
