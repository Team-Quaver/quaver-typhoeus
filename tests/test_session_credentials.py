"""凭证模式单测（无网络）：**本模块（以及整个 sidecar）永远不写出凭证明文**。

跑：cd vendor/Typhoeus && python -m pytest

背景：凭证由 Electron 主进程独占保存（系统密钥管理器 + credential.enc 密文）。
sidecar 只有两种模式，且都不是「写明文」：
    QUAVER_CREDENTIAL_MODE=external → stdin 收凭证 / stdout 交回去，自己绝不落盘；
    不设（手工单跑）= memory        → 只驻内存，关掉即需重新登录。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# 每个用例起一个干净的解释器：session.py 在 import 期就读环境变量与磁盘，同进程里改不了
_HERE = Path(__file__).resolve().parent.parent          # vendor/Typhoeus


def run_sidecar_snippet(code: str, *, env_extra: dict | None = None, stdin: str = "", config_dir: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "QUAVER_CONFIG_DIR": str(config_dir)}
    env.pop("QUAVER_CREDENTIAL_MODE", None)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_HERE),
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=90,
    )


# run.py 的那套日志配置（logger.info 得先有 handler，否则 stderr 上什么都看不到）
_LOGGING = "import logging; logging.basicConfig(level=logging.INFO)\n"


def tmp_config(tmp_path: Path) -> Path:
    d = tmp_path / "quaver-music"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_memory_mode_writes_nothing(tmp_path: Path) -> None:
    """手工单跑（不设模式）：登录成功也不该在磁盘上留下任何凭证文件。"""
    cfg = tmp_config(tmp_path)
    code = (
        _LOGGING
        + "from quaver_server import session as S\n"
        "from qqmusic_api import Credential\n"
        "print('MODE=' + S.credential_mode())\n"
        "S.session.adopt(Credential(musicid=42, musickey='W_X_demo', login_type=1))\n"
        "print('LOGGED_IN=' + str(S.session.logged_in))\n"
    )
    p = run_sidecar_snippet(code, config_dir=cfg)
    assert p.returncode == 0, p.stderr
    assert "MODE=memory" in p.stdout
    assert "LOGGED_IN=True" in p.stdout  # 内存里照常可用
    assert not (cfg / "credential.json").exists(), "memory 模式竟然写出了明文"
    assert not (cfg / "credential.enc").exists(), "sidecar 不该自己产生加密存档（那是主进程的事）"
    # 日志里要有一条明确的告知（用户得知道这次登录不会持久化）
    assert "memory" in p.stderr


def test_memory_mode_does_not_read_leftover_plaintext(tmp_path: Path) -> None:
    """磁盘上就算躺着遗留明文，memory 模式也不许去读（读它就得有落盘口径，一律不碰）。"""
    cfg = tmp_config(tmp_path)
    (cfg / "credential.json").write_text(
        json.dumps({"musicid": 999, "musickey": "W_X_leftover", "loginType": 1}), encoding="utf-8"
    )
    p = run_sidecar_snippet("from quaver_server import session as S\nprint('LOGGED_IN=' + str(S.session.logged_in))\n", config_dir=cfg)
    assert p.returncode == 0, p.stderr
    assert "LOGGED_IN=False" in p.stdout
    assert (cfg / "credential.json").exists(), "sidecar 既不该读也不该删它（交给主进程导入）"


def test_external_mode_hands_off_instead_of_writing(tmp_path: Path) -> None:
    """external：stdin 收、stdout 交回，磁盘上不留东西。"""
    cfg = tmp_config(tmp_path)
    code = (
        "from quaver_server import session as S\n"
        "from qqmusic_api import Credential\n"
        "print('MODE=' + S.credential_mode())\n"
        "print('LOGGED_IN=' + str(S.session.logged_in))\n"
        "S.session.adopt(Credential(musicid=1234, musickey='W_X_handed', login_type=1))\n"
    )
    p = run_sidecar_snippet(
        code,
        env_extra={"QUAVER_CREDENTIAL_MODE": "external"},
        stdin='QCRED1 {"musicid": 1234, "musickey": "W_X_injected", "loginType": 1}\n',
        config_dir=cfg,
    )
    assert p.returncode == 0, p.stderr
    assert "MODE=external" in p.stdout
    assert "LOGGED_IN=True" in p.stdout  # 收下了注入的凭证
    lines = [l for l in p.stdout.splitlines() if l.startswith("QCRED1 ")]
    assert len(lines) == 1, p.stdout
    assert json.loads(lines[0][len("QCRED1 "):])["musickey"] == "W_X_handed"  # 交回的是新凭证
    assert not (cfg / "credential.json").exists()
    # 凭证本身绝不能出现在 stderr / 日志里（主进程会把 stdout 的普通行抄进日志文件）
    assert "W_X_handed" not in p.stderr


def test_external_mode_logout_hands_off_null(tmp_path: Path) -> None:
    cfg = tmp_config(tmp_path)
    code = (
        "import asyncio\n"
        "from quaver_server import session as S\n"
        "from qqmusic_api import Credential\n"
        "S.session.adopt(Credential(musicid=7, musickey='W_X_k', login_type=1))\n"
        "asyncio.run(S.session.logout())\n"
    )
    p = run_sidecar_snippet(code, env_extra={"QUAVER_CREDENTIAL_MODE": "external"}, config_dir=cfg)
    assert p.returncode == 0, p.stderr
    handed = [l for l in p.stdout.splitlines() if l.startswith("QCRED1 ")]
    assert handed[-1].strip() == "QCRED1 null", handed  # 登出 = 交回 null，交给主进程删存档
    assert not (cfg / "credential.json").exists()


def test_sidecar_has_no_plaintext_write_path() -> None:
    """源码级回归护栏：写明文那套代码不该再存在（不然「绝不落明文」只是口头承诺）。"""
    src = (_HERE / "quaver_server" / "session.py").read_text(encoding="utf-8")
    assert "mkstemp" not in src, "写明文用的临时文件那套还在"
    assert "os.replace" not in src, "写明文用的原子替换还在"
    assert "CREDENTIAL_PATH.read_text" not in src, "读明文的老口径还在"
    assert "_load_credential_from_disk" not in src
    # credential.json 只允许出现在「常量定义」与「legacy 搬家元组」这两处
    code_lines = [l.strip() for l in src.splitlines() if "credential.json" in l and not l.strip().startswith("#") and '"""' not in l]
    assert len(code_lines) == 2, code_lines
    assert any("CREDENTIAL_PATH =" in l for l in code_lines)
    assert any("_xdg(" in l for l in code_lines)
