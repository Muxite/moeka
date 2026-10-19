from nanobot.cli.instance_env import load_instance_environment


def test_instance_environment_overrides_credentials_without_execution(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', '/owner')
    monkeypatch.setenv('TEST_INSTANCE_KEY', 'other-instance')
    (tmp_path/'keys.env').write_text('HOME=/bad\nTEST_INSTANCE_KEY="$(touch sentinel)"\n')
    load_instance_environment(tmp_path)
    import os
    assert os.environ['HOME'] == '/owner'
    assert os.environ['TEST_INSTANCE_KEY'] == '$(touch sentinel)'
    assert not (tmp_path/'sentinel').exists()
