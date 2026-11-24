"""Feishu delivery through an administrator-provisioned app or a group webhook.

App credentials stay in the existing lark-cli profile; the panel never reads them.
"""
from __future__ import annotations
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import uuid
from app.services import preferences, webhook_adapter

logger = logging.getLogger(__name__)


def _config() -> dict:
    path = os.getenv("FEISHU_APP_CONFIG", "")
    if not path:
        return {}
    try:
        cfg = json.loads(Path(path).read_text())
        kind = cfg.get("recipient_type")
        prefix = "ou_" if kind == "user" else "oc_" if kind == "chat" else None
        if not prefix or not re.fullmatch(prefix + r"[A-Za-z0-9]+", cfg.get("recipient_id", "")):
            return {}
        if not cfg.get("profile") or not Path(cfg.get("cli_path", "")).is_file():
            return {}
        return cfg
    except (OSError, ValueError, TypeError):
        return {}


def app_status() -> dict:
    cfg = _config()
    return {"configured": bool(cfg), "enabled": bool(cfg) and preferences.load().get("feishu_app_enabled", True),
            "name": cfg.get("bot_name", "飞书应用机器人"),
            "recipient": cfg.get("recipient_label", "已配置接收人") if cfg else ""}


def is_configured() -> bool:
    return app_status()["enabled"] or bool(preferences.get_feishu_webhook_url())


def _send_app(cfg: dict, text: str) -> bool:
    # stdin keeps message content out of the process command line. No shell execution.
    argv = [cfg["cli_path"], "im", "+messages-send", "--profile", cfg["profile"], "--as", "bot",
            "--user-id" if cfg["recipient_type"] == "user" else "--chat-id", cfg["recipient_id"],
            "--text", "-", "--idempotency-key", str(uuid.uuid4()), "--json"]
    env = dict(os.environ)
    env["PATH"] = str(Path(cfg["node_path"]).parent) + os.pathsep + env.get("PATH", "") if cfg.get("node_path") else env.get("PATH", "")
    env.update(LARKSUITE_CLI_NO_UPDATE_NOTIFIER="1", LARKSUITE_CLI_NO_SKILLS_NOTIFIER="1")
    try:
        proc = subprocess.run(argv, input=text, text=True, capture_output=True, timeout=25, env=env)
        ok = proc.returncode == 0 and json.loads(proc.stdout).get("ok") is True
        if not ok:
            logger.warning("Feishu app delivery failed (exit %s); credentials and response withheld", proc.returncode)
        return ok
    except (OSError, ValueError, subprocess.TimeoutExpired):
        logger.warning("Feishu app delivery failed or timed out; response withheld")
        return False


def send(title: str, body: str, *, subtitle: str = "", rich: bool = False) -> bool:
    cfg = _config()
    if cfg and preferences.load().get("feishu_app_enabled", True):
        # Preserve full reports, with explicit bounded chunks below the 30KB API limit.
        text = "\n".join(s for s in (title, subtitle, body) if s)
        chunks = []; chunk = ""; size = 0
        for ch in text:
            width = len(ch.encode("utf-8"))
            if size + width > 24000:
                chunks.append(chunk); chunk = ""; size = 0
            chunk += ch; size += width
        if chunk: chunks.append(chunk)
        if not chunks: return False
        return all(_send_app(cfg, part) for part in chunks)
    url = preferences.get_feishu_webhook_url()
    secret = preferences.get_feishu_webhook_secret()
    if rich:
        return webhook_adapter.send_feishu_card(url, title, subtitle, body, secret)
    return webhook_adapter.send_feishu(url, title, body, secret)
