/** Collapse a gateway outage across concurrent requests and polling retries. */
export function createHttpErrorNotifier(
  notify: (message: string, kind: 'error') => void,
  now: () => number = Date.now,
) {
  let lastGatewayNotice = -Infinity
  return (status: number, message: string) => {
    if (status === 401) return
    if ([502, 503, 504].includes(status)) {
      const timestamp = now()
      if (timestamp - lastGatewayNotice < 30_000) return
      lastGatewayNotice = timestamp
      notify(`服务暂时不可用（HTTP ${status}），请稍候重试。`, 'error')
      return
    }
    notify(message, 'error')
  }
}
