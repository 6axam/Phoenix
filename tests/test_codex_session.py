import json
import os
from pathlib import Path
import shutil
import sys
import pytest
from phoenix.adapters.codex_session import CodexCheckError, check_session

IDENT = 'a04f5046-8ea3-4c04-bda8-49fa089858c5'


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    binary = tmp_path / 'codex'
    binary.write_text(f'''#!{sys.executable}
import json, os, sys, time
for line in sys.stdin:
    request = json.loads(line)
    with open(os.environ['PHOENIX_TEST_CALLS'], 'a') as output:
        output.write(json.dumps(request)+'\\n')
    if request['method'] == 'initialize':
        print(json.dumps({{'id': request['id'], 'result': {{}}}}), flush=True)
    if request['method'] == 'thread/read':
        mode = os.environ.get('PHOENIX_TEST_MODE', '')
        if mode == 'timeout': time.sleep(10)
        elif mode == 'error': print(json.dumps({{'id': request['id'], 'error': {{'message': 'secret'}}}}), flush=True)
        else:
            thread = {{'id': os.environ.get('PHOENIX_TEST_ID', request['params']['threadId']), 'cwd': os.environ['PHOENIX_TEST_CWD'], 'ephemeral': mode == 'ephemeral', 'preview': 'PRIVATE CONVERSATION'}}
            print(json.dumps({{'id': request['id'], 'result': {{'thread': thread}}}}), flush=True)
''')
    binary.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ.get('PATH', ''))
    monkeypatch.setenv('PHOENIX_TEST_CWD', str(tmp_path))
    calls = tmp_path / 'calls.jsonl'; monkeypatch.setenv('PHOENIX_TEST_CALLS', str(calls))
    return calls


def test_explicit_session_check_never_fetches_turns_or_emits_preview(fake_codex, tmp_path):
    result = check_session(IDENT, str(tmp_path))
    assert result['status'] == 'metadata_verified'
    assert result['resume_argv'] == ['codex', 'resume', IDENT]
    assert 'PRIVATE' not in json.dumps(result)
    requests = [json.loads(line) for line in fake_codex.read_text().splitlines()]
    assert requests[-1] == {'id': 2, 'method': 'thread/read', 'params': {'threadId': IDENT, 'includeTurns': False}}
    assert not any(req['method'] in ('thread/resume', 'turn/start') for req in requests)


def test_different_project_or_identifier_and_ephemeral_are_rejected(fake_codex, tmp_path, monkeypatch):
    other = tmp_path / 'other'; other.mkdir()
    monkeypatch.setenv('PHOENIX_TEST_CWD', str(other))
    with pytest.raises(CodexCheckError, match='не совпадает'): check_session(IDENT, str(tmp_path))
    monkeypatch.setenv('PHOENIX_TEST_CWD', str(tmp_path))
    monkeypatch.setenv('PHOENIX_TEST_ID', 'a04f5046-8ea3-4c04-bda8-49fa089858c6')
    with pytest.raises(CodexCheckError, match='ID'): check_session(IDENT, str(tmp_path))
    monkeypatch.setenv('PHOENIX_TEST_ID', IDENT); monkeypatch.setenv('PHOENIX_TEST_MODE', 'ephemeral')
    with pytest.raises(CodexCheckError, match='сохранённую'): check_session(IDENT, str(tmp_path))


def test_missing_errors_and_timeout_do_not_disclose_server_error(fake_codex, tmp_path, monkeypatch):
    with pytest.raises(CodexCheckError): check_session('--last', str(tmp_path))
    assert not fake_codex.exists()
    monkeypatch.setenv('PHOENIX_TEST_MODE', 'error')
    with pytest.raises(CodexCheckError) as exc: check_session(IDENT, str(tmp_path))
    assert 'secret' not in str(exc.value)
    monkeypatch.setenv('PHOENIX_TEST_MODE', 'timeout')
    with pytest.raises(CodexCheckError, match='вовремя'): check_session(IDENT, str(tmp_path), timeout=.2)


def test_cli_check_does_not_open_or_create_phoenix_database(fake_codex, tmp_path, monkeypatch, capsys):
    from phoenix.cli import main
    data = tmp_path / 'data'; monkeypatch.setenv('XDG_DATA_HOME', str(data))
    assert main(['codex-check', IDENT, '--cwd', str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)['session_id'] == IDENT
    assert not data.exists()


def test_real_local_server_unknown_uuid_is_rejected_in_isolated_home(tmp_path, monkeypatch):
    if not shutil.which('codex'): pytest.skip('Codex CLI unavailable')
    for key in ('HOME', 'CODEX_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME'):
        directory = tmp_path / key; directory.mkdir()
        monkeypatch.setenv(key, str(directory))
    with pytest.raises(CodexCheckError, match='не предоставил данные'): check_session(IDENT, str(tmp_path), timeout=10)
    assert not (tmp_path / 'XDG_DATA_HOME' / 'phoenix').exists()


def test_real_server_reads_created_thread_metadata_in_isolation(tmp_path, monkeypatch):
    import subprocess
    from phoenix.adapters.codex_session import _read_response
    executable = shutil.which('codex')
    if not executable: pytest.skip('Codex CLI unavailable')
    for key in ('HOME', 'CODEX_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME'):
        directory = tmp_path / key; directory.mkdir()
        monkeypatch.setenv(key, str(directory))
    process = subprocess.Popen([executable, 'app-server', '--listen', 'stdio://'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    def send(value):
        process.stdin.write((json.dumps(value) + '\n').encode()); process.stdin.flush()
    try:
        send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'phoenix-test', 'version': '0.1'}}})
        _read_response(process, 1, 10)
        send({'method': 'initialized', 'params': {}})
        send({'id': 2, 'method': 'thread/start', 'params': {'cwd': str(tmp_path)}})
        created = _read_response(process, 2, 10)['thread']
        send({'id': 3, 'method': 'thread/read', 'params': {'threadId': created['id'], 'includeTurns': False}})
        thread = _read_response(process, 3, 10)['thread']
        assert thread['id'] == created['id'] and thread['cwd'] == str(tmp_path)
        assert thread['ephemeral'] is False and not thread['turns']
    finally:
        process.terminate(); process.wait(timeout=5)
        process.stdin.close(); process.stdout.close()
