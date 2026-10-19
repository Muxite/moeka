import json
from pathlib import Path

from nanobot.config.instances import clone_instance, create_instance, instance_uuid


def test_clone_keeps_portable_setup_without_identity_state_or_credentials(tmp_path):
    template = Path(__file__).resolve().parents[2] / 'templates/workspace'
    source = tmp_path / 'home/.moeka-source'
    create_instance('source', template=template, workspace=source, home=tmp_path/'home')
    cfg = json.loads((source/'config.json').read_text())
    cfg['providers'] = {'openrouter': {'apiKey':'secret', 'extraHeaders':{'Authorization':'secret'}}}
    cfg['channels'] = {'telegram':{'enabled':True, 'token':'secret'}}
    cfg['tools']['mcpServers'] = {'remote':{'headers':{'Authorization':'secret'}}}
    (source/'config.json').write_text(json.dumps(cfg))
    (source/'memory').mkdir(exist_ok=True)
    (source/'memory/private.txt').write_text('private')
    (source/'keys.env').write_text('SECRET=private')
    target=tmp_path/'home/.moeka-clone'
    clone_instance(source, 'clone', workspace=target, home=tmp_path/'home')
    cloned=json.loads((target/'config.json').read_text())
    assert instance_uuid(source) != instance_uuid(target)
    assert cloned['providers'] == {}
    assert not cloned['channels'].get('telegram')
    assert 'mcpServers' not in cloned['tools']
    assert not (target/'memory/private.txt').exists()
    assert (target/'keys.env').read_text() == ''
    assert 'secret' not in (target/'config.json').read_text()
