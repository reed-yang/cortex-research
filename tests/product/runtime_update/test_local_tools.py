"""Exercise useful local commands and retained boundaries in real processes."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from cortex_platform.product.config import ConfigError, validate_config
from cortex_platform.product.runtime_update.sandbox import (
    SANDBOX_EXEC, SandboxError, build_policy, prepare_sandbox, render_profile,
)
from cortex_platform.product.runtime_update.supervisor import WorkerSupervisorV2
from cortex_platform.product.runtime_update.worker_payload.cortex_worker import tool_policy
from cortex_platform.product.runtime_update.worker_payload import module_sources
from cortex_platform.product.runtime_update.worker_protocol import SlotInterpreterDescriptor
from cortex_platform.runtime.managed_hermes import ManagedHermesBackend, ManagedRuntimeUnavailable

from .test_worker_v2 import _descriptor


def test_local_tool_configuration_is_explicit():
    for value in ("none", "local"):
        assert validate_config({"config_version": 1, "runtime": {"tools": value}})["runtime"]["tools"] == value
    for value in ("all", "unrestricted", True):
        with pytest.raises(ConfigError):
            validate_config({"config_version": 1, "runtime": {"tools": value}})


@pytest.mark.skipif(platform.system() != "Darwin", reason="Darwin sandbox")
def test_local_commands_read_libraries_without_modifying_them(tmp_path):
    descriptor_path, descriptor = _descriptor(tmp_path)
    library = tmp_path / "papers"
    library.mkdir()
    notes = library / "notes.md"
    notes.write_text("retained notes\n")
    outside = tmp_path / "private.txt"
    outside.write_text("private fixture\n")
    (library / "escape").symlink_to(outside)
    launch = prepare_sandbox(descriptor, descriptor_path=descriptor_path,
                             local_tools=True, tool_read_roots=(str(library),))
    assert dict(launch.probes)["spawn_shell"] == "allowed"
    supervisor = WorkerSupervisorV2(descriptor_path, sandbox=launch)
    environment = supervisor._environment()
    workspace = Path(environment["TERMINAL_CWD"])
    script = r'''
import json,subprocess,sys
library,outside,home,workspace=sys.argv[1:]
commands={
 'pwd':['/bin/pwd'],
 'read':['/bin/cat',library+'/notes.md'],
 'write_workspace':['/bin/sh','-c','echo writable > output.txt; cat output.txt'],
 'write_library':['/bin/sh','-c','echo changed > "$1/notes.md"','sh',library],
 'delete_library':['/bin/rm',library+'/notes.md'],
 'read_outside':['/bin/cat',outside],
 'read_escape':['/bin/cat',library+'/escape'],
 'write_hook':['/bin/sh','-c','echo bad > "$1/hooks/plugin.py"','sh',home],
 'read_auth':['/bin/cat',home+'/auth.json'],
}
for name,argv in commands.items():
 p=subprocess.run(argv,cwd=workspace,capture_output=True,text=True)
 print(json.dumps({'name':name,'returncode':p.returncode,'output':p.stdout}))
'''
    home = descriptor.state_dir / "hermes-home"
    (home / "hooks").mkdir()
    (home / "auth.json").write_text('{"test":"private"}')
    completed = subprocess.run(launch.wrap([
        str(descriptor.interpreter_path), "-I", "-c", script,
        str(library), str(outside), str(home), str(workspace),
    ]), env=environment, cwd=workspace, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    results = {r["name"]: r for r in map(json.loads, completed.stdout.splitlines())}
    for name in ("pwd", "read", "write_workspace"):
        assert results[name]["returncode"] == 0, results[name]
    for name in ("write_library", "delete_library", "read_outside", "read_escape", "write_hook", "read_auth"):
        assert results[name]["returncode"] != 0, results[name]
    assert notes.read_text() == "retained notes\n"
    assert (workspace / "output.txt").read_text() == "writable\n"

    canceled = subprocess.run(launch.wrap([
        str(descriptor.interpreter_path), "-I", "-c",
        "import subprocess; p=subprocess.Popen(['/bin/sleep','20']); p.terminate(); p.wait(timeout=3)",
    ]), env=environment, cwd=workspace, capture_output=True, text=True, timeout=5)
    assert canceled.returncode == 0, canceled.stderr


def test_library_roots_do_not_grant_broad_or_implicit_access(tmp_path):
    path, descriptor = _descriptor(tmp_path)
    for root in ("/", str(Path.home()), str(tmp_path / "missing"), "relative"):
        with pytest.raises(SandboxError):
            build_policy(descriptor, descriptor_path=path, local_tools=True, tool_read_roots=(root,))
    with pytest.raises(SandboxError):
        build_policy(descriptor, descriptor_path=path, tool_read_roots=(str(tmp_path),))


def test_local_mode_refuses_an_old_worker_before_any_turn(tmp_path, monkeypatch):
    path, _ = _descriptor(tmp_path)
    backend = ManagedHermesBackend(path, local_tools=True)
    closed = []

    class OldWorker:
        def start(self):
            pass

        def request(self, method, params):
            assert method == "health.check"
            return {"healthy": True, "ledger_open": True}

        def close(self, *, force):
            closed.append(force)

    monkeypatch.setattr(backend, "_run_dedup_probe", lambda: None)
    monkeypatch.setattr(backend, "_new_supervisor", lambda _: OldWorker())
    with pytest.raises(ManagedRuntimeUnavailable, match="require_worker_policy"):
        backend._launch()
    assert closed == [True]


@pytest.mark.parametrize("form", ["inherited", "keyword", "positional"])
def test_subprocesses_never_inherit_provider_transport_or_protocol_secrets(tmp_path, form):
    script = '''
import json,os,subprocess,sys
sys.path.insert(0,sys.argv[1])
from tool_policy import install_subprocess_environment
install_subprocess_environment()
install_subprocess_environment()
args=[sys.executable,'-I','-c','import json,os;print(json.dumps(dict(os.environ)))']
form=sys.argv[2]
if form=='positional':
 p=subprocess.Popen(args,-1,None,None,subprocess.PIPE,None,None,True,False,None,dict(os.environ),text=True)
else:
 kw={'env':dict(os.environ)} if form=='keyword' else {}
 p=subprocess.Popen(args,stdout=subprocess.PIPE,text=True,**kw)
out,_=p.communicate(timeout=10)
assert p.returncode==0
print(out)
'''
    env = dict(os.environ, CORTEX_WORKER_TOKEN="protocol-fixture",
               OPENAI_API_KEY="provider-fixture", TELEGRAM_BOT_TOKEN_RESEARCH="transport-fixture",
               UNRECOGNIZED_SECRET="secret-fixture", _HERMES_FORCE_OPENAI_API_KEY="override-fixture")
    result = subprocess.run([sys.executable, "-I", "-c", script,
                             str(Path(tool_policy.__file__).parent), form],
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    child = json.loads(result.stdout)
    assert child["PATH"] == env["PATH"]
    assert not any("fixture" in value for value in child.values())


def test_permanent_terminal_denial_does_not_retry(monkeypatch):
    from types import ModuleType

    calls = []
    module = ModuleType("tools.environments.local")

    class LocalEnvironment:
        def execute(self, *args, **kwargs):
            calls.append(args)
            raise PermissionError(1, "Operation not permitted")

    module.LocalEnvironment = LocalEnvironment
    monkeypatch.setitem(sys.modules, "tools.environments.local", module)
    tool_policy.install_terminal_denials()
    result = LocalEnvironment().execute("pwd")
    assert result["returncode"] == 126
    assert "policy" in result["output"]
    assert len(calls) == 1


@pytest.mark.skipif(platform.system() != "Darwin", reason="Darwin sandbox")
def test_real_hermes_file_and_terminal_tools_in_local_policy(hermes_tool_root):
    tmp_path = hermes_tool_root
    supplied = os.environ.get("CORTEX_TEST_HERMES_DESCRIPTOR")
    if not supplied:
        pytest.skip("set CORTEX_TEST_HERMES_DESCRIPTOR for the installed fork tool acceptance")
    original = SlotInterpreterDescriptor.load(Path(supplied))
    descriptor = replace(original, state_dir=tmp_path / "state")
    descriptor_path = tmp_path / "descriptor.json"
    descriptor_path.write_text("{}")
    library = tmp_path / "papers"
    library.mkdir()
    (library / "notes.md").write_text("paper fixture\n")
    launch = prepare_sandbox(descriptor, descriptor_path=descriptor_path,
                             local_tools=True, tool_read_roots=(str(library),))
    fixture = descriptor.state_dir / "fixture"
    for relative, source in module_sources().items():
        target = fixture / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    workspace = descriptor.state_dir / "workspace"
    workspace.mkdir()
    environment = {
        "HOME": str(descriptor.state_dir), "HERMES_HOME": str(descriptor.state_dir / "hermes-home"),
        "TMPDIR": str(workspace), "TERMINAL_CWD": str(workspace),
        "PATH": str(descriptor.interpreter_path.parent) + os.pathsep + os.defpath,
        "HERMES_INTERACTIVE": "1", "CORTEX_LOCAL_TOOLS": "1",
        "CORTEX_WORKER_TOKEN": "private-protocol-fixture",
        "TELEGRAM_BOT_TOKEN_RESEARCH": "private-transport-fixture",
        "OPENAI_API_KEY": "private-provider-fixture",
    }
    script = '''
import json,os,sys
from pathlib import Path
sys.dont_write_bytecode=True
sys.path[:0]=[sys.argv[1],sys.argv[2]]
from cortex_worker.runtime import load_runtime
_,_,set_approval=load_runtime(Path(os.environ['HERMES_HOME']))
set_approval(lambda *args,**kwargs:'once')
from tools.file_tools import search_tool,read_file_tool,write_file_tool
from tools.terminal_tool import terminal_tool
checks={
 'terminal':terminal_tool(command='pwd'),
 'search':search_tool(pattern='*notes*',target='files',path=sys.argv[3]),
 'read':read_file_tool(path=sys.argv[3]+'/notes.md'),
 'write':write_file_tool(path='tool-output.txt',content='workspace output'),
 'child':terminal_tool(command="python3 -c \\"import os; print(any(k in os.environ for k in ['CORTEX_WORKER_TOKEN','TELEGRAM_BOT_TOKEN_RESEARCH','OPENAI_API_KEY']))\\""),
}
for name,result in checks.items():print('RESULT',json.dumps({'name':name,'result':json.loads(result)}))
'''
    result = subprocess.run(launch.wrap([
        str(descriptor.interpreter_path), "-I", "-B", "-c", script,
        str(fixture), str(descriptor.slot_path / "content"), str(library),
    ]), env=environment, cwd=workspace, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    records = [json.loads(line.removeprefix("RESULT ")) for line in result.stdout.splitlines()
               if line.startswith("RESULT ")]
    checks = {record["name"]: record["result"] for record in records}
    assert checks["terminal"]["exit_code"] == 0
    assert checks["search"]["total_count"] == 1
    assert "paper fixture" in checks["read"]["content"]
    assert (workspace / "tool-output.txt").exists(), checks["write"]
    assert (workspace / "tool-output.txt").read_text() == "workspace output"
    assert checks["child"]["exit_code"] == 0, checks["child"]
    assert checks["child"]["output"].strip() == "False"


@pytest.fixture
def hermes_tool_root():
    # Hermes deliberately refuses file-tool writes below /private/var, where
    # macOS places pytest's default temporary root. Use an ordinary scratch root.
    with tempfile.TemporaryDirectory(prefix="cortex-local-tools-", dir="/tmp") as value:
        yield Path(value).resolve()
