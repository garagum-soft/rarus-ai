"""The build gate finds key shapes and internal addresses, and leaves placeholders alone."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_build.py"
spec = importlib.util.spec_from_file_location("check_build", SCRIPT)
check_build = importlib.util.module_from_spec(spec)
sys.modules["check_build"] = check_build
spec.loader.exec_module(check_build)

REAL_KEY = "sk-" + "Zq8rT2vXw9LmN4bC7dF1gH5jK3pS6" + "uY"


def write(tmp_path: Path, name: str, content: bytes | str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    return path


def names(findings) -> list[str]:
    return sorted(f.what for f in findings)


def test_a_clean_tree_passes(tmp_path):
    write(tmp_path, "a.py", "print('hello')\nurl = 'https://alembox.com/'\n")
    assert check_build.scan([tmp_path]) == []
    assert check_build.main([str(tmp_path)]) == 0


@pytest.mark.parametrize(
    ("text", "what"),
    [
        (f"key = '{REAL_KEY}'", "provider key (sk-...)"),
        ("k = 'gsk_" + "a1B2c3D4e5F6g7H8i9J0k1L2" + "'", "provider key (gsk_...)"),
        ("k = 'AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6" + "'", "Google key (AIza...)"),
        ("k = 'sk-ab-" + "9f8e7d6c5b4a39281706f5e4d3c2b1a0" + "'", "agent key (sk-ab-...)"),
        ("secret = 'dev-secret'", "dev secret"),
        ("host = '192.168.1.20'", "private network address (192.168.)"),
        ("host = '10.0.3.7'", "private network address (10.)"),
        ("ssh 95.85.125.50", "the server's address"),
        ("proxy = '127.0.0.1:8115'", "an internal server port"),
    ],
)
def test_it_finds_each_shape(tmp_path, text, what):
    write(tmp_path, "src/m.py", text)
    assert names(check_build.scan([tmp_path])) == [what]
    assert check_build.main([str(tmp_path)]) == 1


@pytest.mark.parametrize(
    "text",
    [
        "sk-ab-" + "0123456789abcdef" * 3,
        "sk-" + "x" * 30,
        "sk-" + "A" * 30,
        "sk-ab-xxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "gsk_" + "0" * 30,
        "sk-ab-...",
        "the key starts with sk-ab- and is shown once",
        "version 10.2 and 110.4.1.2.3 and 5.10.0.1",
        "127.0.0.1:18792 and 127.0.0.1:47821",
    ],
)
def test_placeholders_and_lookalikes_pass(tmp_path, text):
    write(tmp_path, "docs.md", text)
    assert check_build.scan([tmp_path]) == []


def test_it_reads_binary_files_for_keys_and_the_server_address_but_not_for_numeric_runs(tmp_path):
    noise = b"\x00\x01" + b"10.1.2.3" + b"\x02" + b"192.168.0.9" + b"\x00"
    write(tmp_path, "tool.exe", noise)
    assert check_build.scan([tmp_path]) == []
    write(tmp_path, "tool2.exe", b"\x00" + REAL_KEY.encode() + b"\x00" + b"95.85.125.50")
    assert names(check_build.scan([tmp_path])) == ["provider key (sk-...)", "the server's address"]


def test_a_finding_never_prints_the_whole_key(tmp_path):
    write(tmp_path, "m.py", REAL_KEY)
    [finding] = check_build.scan([tmp_path])
    assert REAL_KEY not in str(finding)
    assert str(finding).endswith("sk-Zq8rT...")


def test_it_skips_caches_and_named_junk_dirs(tmp_path):
    write(tmp_path, "__pycache__/m.pyc", REAL_KEY)
    write(tmp_path, "node_modules/x/index.js", REAL_KEY)
    assert check_build.scan([tmp_path]) == []


def test_a_single_file_and_a_missing_path(tmp_path, capsys):
    file = write(tmp_path, "one.txt", "dev-secret")
    assert check_build.main([str(file)]) == 1
    assert check_build.main([str(tmp_path / "nope")]) == 2
    assert check_build.main([]) == 2
    assert "no such path" in capsys.readouterr().err
