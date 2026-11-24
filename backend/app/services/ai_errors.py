"""Safe, actionable AI error messages without provider bodies or credentials."""


def ai_error_message(error: Exception) -> str:
    status = getattr(error, 'status_code', None)
    if status == 401:
        return 'AI认证失败（401）：请在设置 → AI 中检查 API 密钥。'
    if status == 403:
        return 'AI模型权限不足或模型ID无效（403）：请在设置 → AI 中选择当前密钥可用的模型。'
    if status == 404:
        return 'AI接口或模型不存在（404）：请检查模型ID与接口地址。'
    if status == 429:
        return 'AI服务限流或额度不足（429）：请检查供应商额度，稍后重试。'
    if isinstance(status, int) and status >= 500:
        return f'AI供应商服务异常（{status}）：请稍后重试。'
    kind = type(error).__name__
    if isinstance(error, TimeoutError) or 'Timeout' in kind:
        return 'AI请求超时：供应商尚未返回，请稍后重试或检查模型隧道。'
    if kind in {'APIConnectionError', 'ConnectError', 'ConnectTimeout', 'NetworkError'}:
        return 'AI网络连接失败：请检查远端模型隧道及网络连接。'
    return 'AI请求失败：请检查模型配置与服务日志（错误详情不回显）。'
