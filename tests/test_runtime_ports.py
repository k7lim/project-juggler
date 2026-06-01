from __future__ import annotations

import json
import subprocess
from unittest import mock

import pytest

from pj import cli, runtime_ports


LSOF_OUTPUT = """\
COMMAND   PID USER   FD   TYPE DEVICE SIZE/OFF NODE NAME
node    12345 kev    22u  IPv4 123456      0t0  TCP 127.0.0.1:3000 (LISTEN)
python  23456 kev     5u  IPv6 234567      0t0  TCP [::1]:8000 (LISTEN)
"""


SS_OUTPUT = """\
State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process
LISTEN 0      128        127.0.0.1:5173      0.0.0.0:* users:(("vite",pid=34567,fd=21))
LISTEN 0      4096               *:8080            *:* users:(("python",pid=45678,fd=3))
"""


PROC_TCP_OUTPUT = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  501        0 1 1 0000000000000000 100 0 0 10 0
   1: 00000000:0BB8 00000000:0000 01 00000000:00000000 00:00000000 00000000  501        0 2 1 0000000000000000 100 0 0 10 0
"""


def test_parse_lsof_normalizes_listening_ports():
    records = runtime_ports.parse_lsof(LSOF_OUTPUT)

    assert records[0] == {
        "project_id": None,
        "path": None,
        "live_urls": ["http://127.0.0.1:3000/"],
        "pid": 12345,
        "port": 3000,
        "host": "127.0.0.1",
        "command": "node",
        "cwd": None,
        "confidence": "unknown",
        "source": "lsof",
    }
    assert records[1]["host"] == "::1"
    assert records[1]["live_urls"] == ["http://[::1]:8000/"]


def test_parse_ss_normalizes_process_metadata_and_wildcard_hosts():
    records = runtime_ports.parse_ss(SS_OUTPUT)

    assert records[0]["command"] == "vite"
    assert records[0]["pid"] == 34567
    assert records[0]["port"] == 5173
    assert records[1]["host"] == "*"
    assert records[1]["live_urls"] == ["http://127.0.0.1:8080/", "http://localhost:8080/"]


def test_parse_proc_net_tcp_returns_listen_rows_only():
    records = runtime_ports.parse_proc_net_tcp(PROC_TCP_OUTPUT)

    assert len(records) == 1
    assert records[0]["host"] == "127.0.0.1"
    assert records[0]["port"] == 8080
    assert records[0]["pid"] is None
    assert records[0]["source"] == "procfs"


def test_pid_cwd_uses_lsof_cwd_when_procfs_is_unavailable():
    def runner(argv):
        assert argv == ["lsof", "-a", "-p", "23456", "-d", "cwd", "-Fn"]
        return subprocess.CompletedProcess(argv, 0, "p23456\nn/work/demo\n", "")

    with mock.patch("pj.runtime_ports.os.readlink", side_effect=OSError):
        assert runtime_ports._pid_cwd(23456, runner=runner) == "/work/demo"


def test_discover_ports_associates_lsof_cwd_inside_project_on_macos():
    project = {"id": "abc123", "name": "demo", "path": "/work/demo"}

    def runner(argv):
        if argv[:4] == ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]:
            return subprocess.CompletedProcess(argv, 0, LSOF_OUTPUT, "")
        if argv == ["lsof", "-a", "-p", "12345", "-d", "cwd", "-Fn"]:
            return subprocess.CompletedProcess(argv, 0, "p12345\nn/work/demo\n", "")
        if argv == ["lsof", "-a", "-p", "23456", "-d", "cwd", "-Fn"]:
            return subprocess.CompletedProcess(argv, 0, "p23456\nn/tmp\n", "")
        return subprocess.CompletedProcess(argv, 1, "", "no ss")

    with mock.patch("pj.runtime_ports.os.readlink", side_effect=OSError), \
         mock.patch("pj.runtime_ports.Path.exists", return_value=False):
        records, meta = runtime_ports.discover_ports(project=project, runner=runner)

    assert meta["warnings"] == ["ss exited 1: no ss"]
    assert len(records) == 1
    assert records[0]["project_id"] == "abc123"
    assert records[0]["path"] == "/work/demo"
    assert records[0]["cwd"] == "/work/demo"
    assert records[0]["confidence"] == "high"


def test_discover_ports_associates_cwd_inside_project_as_high_confidence():
    project = {"id": "abc123", "name": "demo", "path": "/work/demo"}

    def runner(argv):
        if argv[0] == "lsof":
            return subprocess.CompletedProcess(argv, 0, LSOF_OUTPUT, "")
        return subprocess.CompletedProcess(argv, 1, "", "no ss")

    with mock.patch("pj.runtime_ports._pid_cwd", return_value="/work/demo/src"), \
         mock.patch("pj.runtime_ports.Path.exists", return_value=False):
        records, meta = runtime_ports.discover_ports(project=project, runner=runner)

    assert meta["warnings"] == ["ss exited 1: no ss"]
    assert records[0]["project_id"] == "abc123"
    assert records[0]["path"] == "/work/demo"
    assert records[0]["confidence"] == "high"


def test_discover_ports_prefers_most_specific_containing_project_path():
    projects = [
        {"id": "parent", "name": "kevin", "path": "/Users/kevin"},
        {
            "id": "child",
            "name": "subreddit-analysis",
            "path": "/Users/kevin/Development/sandbox/projects/subreddit-analysis",
        },
    ]

    def runner(argv):
        if argv[0] == "lsof":
            return subprocess.CompletedProcess(argv, 0, LSOF_OUTPUT, "")
        return subprocess.CompletedProcess(argv, 1, "", "no ss")

    with mock.patch(
        "pj.runtime_ports._pid_cwd",
        return_value="/Users/kevin/Development/sandbox/projects/subreddit-analysis",
    ), mock.patch("pj.runtime_ports.Path.exists", return_value=False):
        records, meta = runtime_ports.discover_ports(projects=projects, runner=runner)

    assert meta["warnings"] == ["ss exited 1: no ss"]
    assert records[0]["project_id"] == "child"
    assert records[0]["path"] == "/Users/kevin/Development/sandbox/projects/subreddit-analysis"
    assert records[0]["confidence"] == "high"


def test_ports_facade_returns_empty_error_envelope_for_unexpected_failure():
    def boom(*, project=None, runner=None):
        raise RuntimeError("discovery exploded")

    with mock.patch("pj.runtime_ports.discover_ports", side_effect=boom):
        env = runtime_ports.ports()

    assert env["success"] is False
    assert env["data"] == []
    assert env["meta"]["error"] == "discovery exploded"
    assert env["meta"]["source"] == "ports"


def test_ports_facade_missing_project_uses_standard_error_envelope():
    with mock.patch("pj.runtime_ports.discover.resolve_project", return_value=None):
        env = runtime_ports.ports("missing")

    assert env["success"] is False
    assert env["data"] == []
    assert env["meta"]["error"] == "No project matching 'missing'"


def test_cli_ports_outputs_envelope(capsys):
    payload = {"success": True, "data": [], "meta": {"total": 0, "sources": [], "warnings": []}}
    with mock.patch("pj.cli.runtime_ports.ports", return_value=payload) as ports:
        cli.main(["ports", "--project", "demo"])

    ports.assert_called_once_with("demo")
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == payload


def test_cli_ports_pretty_outputs_table(capsys):
    payload = {
        "success": True,
        "data": [
            {
                "project_id": "abc123",
                "path": "/work/demo",
                "live_urls": ["http://127.0.0.1:3000/"],
                "pid": 12345,
                "port": 3000,
                "host": "127.0.0.1",
                "command": "node",
                "cwd": "/work/demo",
                "confidence": "high",
                "source": "lsof",
            }
        ],
        "meta": {"total": 1, "sources": ["lsof"], "warnings": []},
    }
    with mock.patch("pj.cli.runtime_ports.ports", return_value=payload) as ports:
        cli.main(["ports", "--project", "demo", "--pretty"])

    ports.assert_called_once_with("demo")
    out = capsys.readouterr().out
    assert "PORT" in out
    assert "demo" in out
    assert "http://127.0.0.1:3000/" in out


def test_cli_ports_exits_nonzero_on_error(capsys):
    payload = {"success": False, "data": [], "meta": {"error": "No project matching 'missing'"}}
    with mock.patch("pj.cli.runtime_ports.ports", return_value=payload), \
         pytest.raises(SystemExit) as exc:
        cli.main(["ports", "--project", "missing"])

    assert exc.value.code == 1
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == payload
