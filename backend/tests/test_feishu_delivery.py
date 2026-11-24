import json
import subprocess
from types import SimpleNamespace
from app.services import feishu_delivery as delivery


def configured(monkeypatch, tmp_path):
    cli = tmp_path / 'lark-cli'; cli.write_text('unused')
    cfg = dict(profile='existing-app', recipient_type='chat', recipient_id='oc_test123',
               bot_name='全球股市研究助手', cli_path=str(cli), node_path='/runtime/node')
    path = tmp_path / 'app.json'; path.write_text(json.dumps(cfg))
    monkeypatch.setenv('FEISHU_APP_CONFIG', str(path))
    monkeypatch.setattr(delivery.preferences, 'load', lambda: {})
    return cfg


def test_app_message_uses_fixed_bot_identity_and_stdin(monkeypatch, tmp_path):
    cfg = configured(monkeypatch, tmp_path); seen = []
    def run(argv, **kw):
        seen.append((argv, kw)); return SimpleNamespace(returncode=0, stdout='{"ok":true}')
    monkeypatch.setattr(delivery.subprocess, 'run', run)
    assert delivery.send('告警', '正文')
    argv, kw = seen[0]
    assert argv[argv.index('--profile')+1] == cfg['profile']
    assert argv[argv.index('--as')+1] == 'bot'
    assert argv[argv.index('--chat-id')+1] == cfg['recipient_id']
    assert kw['input'] == '告警\n正文' and '正文' not in argv
    assert 'recipient_id' not in delivery.app_status()


def test_business_failure_and_timeout_do_not_report_success(monkeypatch, tmp_path):
    configured(monkeypatch, tmp_path)
    monkeypatch.setattr(delivery.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='{"ok":false}'))
    assert not delivery.send('test', 'body')
    def timeout(*a, **k): raise subprocess.TimeoutExpired('cli', 25)
    monkeypatch.setattr(delivery.subprocess, 'run', timeout)
    assert not delivery.send('test', 'body')


def test_utf8_reports_are_split_without_loss(monkeypatch, tmp_path):
    configured(monkeypatch, tmp_path); chunks=[]
    monkeypatch.setattr(delivery, '_send_app', lambda cfg, text: chunks.append(text) or True)
    body='报告正文'*6000
    assert delivery.send('报告', body, rich=True)
    assert ''.join(chunks) == '报告\n'+body
    assert all(len(c.encode()) <= 24000 for c in chunks)


def test_disabled_app_preserves_existing_group_webhook(monkeypatch, tmp_path):
    configured(monkeypatch, tmp_path)
    monkeypatch.setattr(delivery.preferences, 'load', lambda: {'feishu_app_enabled':False})
    monkeypatch.setattr(delivery.preferences, 'get_feishu_webhook_url', lambda:'existing-hook')
    monkeypatch.setattr(delivery.preferences, 'get_feishu_webhook_secret', lambda:'')
    sent=[]
    monkeypatch.setattr(delivery.webhook_adapter, 'send_feishu_card', lambda *args: sent.append(args) or True)
    assert delivery.send('title', 'body', subtitle='date', rich=True)
    assert sent == [('existing-hook','title','date','body','')]
