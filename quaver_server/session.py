"""Quaver 本地 API sidecar — 基于 L-1124/QQMusicApi 的薄适配层.

会话/凭证：
- 配置根目录与 Electron 主进程共用**同一套平台规则**（真相在 ui/electron/config.mjs，
  改一边必须改两边）：
      Linux    $XDG_CONFIG_HOME/quaver-music   默认 ~/.config/quaver-music
      Windows  %AppData%/Quaver Music
      macOS    ~/Library/Application Support/Quaver Music
  Electron 拉起 sidecar 时会显式下传 QUAVER_CONFIG_DIR。
- **凭证由 Electron 主进程独占，本模块从不读写凭证明文。** 磁盘上只有密文 credential.enc，
  钥匙在系统密钥管理器里（KWallet / GNOME Keyring / Windows 凭据管理器 / macOS 钥匙串，
  见 ui/electron/keyring.mjs）。启动时主进程通过 stdin 注入一行 `QCRED1 {json}`
  （或 `QCRED1 null`），本模块登录/刷新/登出时往 stdout 回写同一格式，由主进程加密落盘。
  这个模式由环境变量 QUAVER_CREDENTIAL_MODE=external 声明。
- **不设该变量（手工单跑 `uv run run.py`）＝ memory 模式**：不落盘、也不读盘，
  登录只在本次进程内存里活着。手工起的 sidecar 拿不到与主进程之间的交接管道，
  而明文凭证已不允许存在 —— 所以宁可这次不持久化，也不写明文。
- device.json 存 SDK 设备指纹（跨重启保号），与配置同目录；它不是密钥，保持明文。
- 同一目录里还有客户端设置 quaver.conf（INI），那是 Electron 侧的文件，本模块不碰。
- 旧路径（~/.config/quaver、~/.local/state/quaver）的文件在首次访问时搬过来，避免丢登录态。
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from qqmusic_api import Client, Credential
from qqmusic_api.core.exceptions import CredentialInvalidError

logger = logging.getLogger("quaver.session")

# 凭证交接信封前缀：与 ui/electron/keyring.mjs:HANDOFF_PREFIX 必须**逐字一致**（含末尾空格）。
# stdout 上只有带这个前缀的行会被主进程当凭证，其余按普通日志处理。
HANDOFF_PREFIX = "QCRED1 "

# 父进程（Electron 主进程）是否接管凭证的持久化。见模块 docstring。
# 只有两个状态：external（交接给主进程）与 memory（不落盘）。**没有 file** ——
# 明文凭证不在选项里（本模块连写明文那段代码都不该存在）。
EXTERNAL_CREDENTIALS = os.environ.get("QUAVER_CREDENTIAL_MODE", "").strip().lower() == "external"


def credential_mode() -> str:
    """当前凭证模式：external=交给主进程（系统密钥管理器）| memory=只驻内存。"""
    return "external" if EXTERNAL_CREDENTIALS else "memory"


def _xdg(base_env: str, default: str) -> Path:
    return Path(os.environ.get(base_env, str(Path.home() / default))).expanduser()


def _config_dir() -> Path:
    """配置根目录（与 ui/electron/config.mjs:configDir 保持一致）."""
    override = os.environ.get("QUAVER_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", "").strip() or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Quaver Music"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Quaver Music"
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return Path(xdg) / "quaver-music" if xdg else Path.home() / ".config" / "quaver-music"


CONFIG_DIR = _config_dir()
CREDENTIAL_PATH = CONFIG_DIR / "credential.json"
DEVICE_PATH = CONFIG_DIR / "device.json"

# 迁移前的老位置（凭证在 XDG_CONFIG_HOME/quaver，设备指纹在 XDG_STATE_HOME/quaver）
_LEGACY_PATHS = (
    (_xdg("XDG_CONFIG_HOME", ".config") / "quaver" / "credential.json", CREDENTIAL_PATH),
    (_xdg("XDG_STATE_HOME", ".local/state") / "quaver" / "device.json", DEVICE_PATH),
)


def migrate_legacy_files() -> None:
    """把老目录里的凭证/设备指纹搬进新配置目录。目标已存在就不动（只搬一次）。

    搬凭证的意义只剩下「让主进程能看见它」：本模块自己不会去读盘，但主进程启动时会从
    新路径把遗留明文导入密钥环并删除（见 ui/electron/keyring.mjs:migrateLegacy）。
    这里只是**搬家**，不会新建明文。
    """
    for old, new in _LEGACY_PATHS:
        try:
            if new.exists() or not old.exists() or old.resolve() == new.resolve():
                continue
            new.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.move(str(old), str(new))  # 可能跨设备（Windows 上 .config 与 %AppData% 不同盘）
            os.chmod(new, stat.S_IRUSR | stat.S_IWUSR)
            logger.info("已迁移凭证文件 %s -> %s", old, new)
        except OSError:
            logger.warning("迁移 %s 失败（忽略，按未登录处理）", old, exc_info=True)


migrate_legacy_files()


def credential_has_login(credential: Credential) -> bool:
    """Credential 是否含可用登录信息（同上游 web 层判定）."""
    return credential.musicid > 0 and bool(credential.musickey)


def _emit_credential(credential: Credential | None) -> None:
    """把凭证交给父进程（Electron 主进程）加密落盘；null 表示登出。

    stdout 是 sidecar 的日志通道，主进程按前缀分流 —— 所以这一行必须单行、必须立刻 flush
    （stdout 走管道时是块缓冲，不 flush 会一直睡在缓冲区里），且绝不能同时写进 stderr / 日志
    —— 那边会被主进程原样落进 electron-dev.log，等于把 musickey 抄进日志文件。
    """
    payload = credential.model_dump_json() if credential is not None else "null"
    try:
        sys.stdout.write(f"{HANDOFF_PREFIX}{payload}\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        logger.warning("凭证交接失败（stdout 不可写），本次登录不会被持久化")


def _read_injected_credential() -> Credential:
    """读主进程注入的第一行凭证。

    只在 external 模式下调用：主进程 spawn 完会立刻写入这一行，所以这里的阻塞读不会真的等。
    反过来说，**memory 模式绝不能调它** —— 手工在终端跑 sidecar 时会卡在 stdin 上等输入。
    """
    try:
        line = sys.stdin.readline()
    except (OSError, ValueError):
        logger.warning("读取注入凭证失败（stdin 不可读），按未登录处理")
        return Credential()
    if not line:
        logger.info("父进程未注入凭证（stdin 立即 EOF），按未登录处理")
        return Credential()
    body = line.strip()
    if not body.startswith(HANDOFF_PREFIX):
        logger.warning("注入行前缀不匹配，按未登录处理")
        return Credential()
    payload = body[len(HANDOFF_PREFIX):].strip()
    if payload == "null":
        return Credential()
    try:
        cred = Credential.model_validate_json(payload)
    except Exception:
        logger.exception("注入的凭证解析失败，按未登录处理")
        return Credential()
    if credential_has_login(cred):
        logger.info("已接收父进程注入的登录凭证 musicid=%s", cred.musicid)
    return cred


def _initial_credential() -> Credential:
    """启动时的凭证来源：external=主进程注入 | memory=没有（本次不持久化）。"""
    if EXTERNAL_CREDENTIALS:
        return _read_injected_credential()
    logger.info(
        "凭证模式=memory（未设 QUAVER_CREDENTIAL_MODE）：登录只在内存里，关掉本进程即需重新登录"
        "；要持久化请由 Electron 主进程拉起 sidecar（它会走 stdin/stdout 交接）"
    )
    return Credential()


def save_credential(credential: Credential) -> None:
    """external 模式交给主进程加密保存；memory 模式下什么都不做（**绝不写明文**）。"""
    if EXTERNAL_CREDENTIALS:
        _emit_credential(credential)


def clear_credential() -> None:
    if EXTERNAL_CREDENTIALS:
        _emit_credential(None)


class Session:
    """进程级 SDK Client 封装（凭证变更集中处理）."""

    def __init__(self) -> None:
        DEVICE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self.client = Client(credential=_initial_credential(), device_path=str(DEVICE_PATH))
        self._listeners: list[Callable[[], None]] = []

    def add_change_listener(self, cb) -> None:
        """登录态变更回调（adopt/logout 后触发）。供 Typhoeus 等下游清缓存。"""
        self._listeners.append(cb)

    def _notify(self) -> None:
        for cb in self._listeners:
            try:
                cb()
            except Exception:
                logger.warning("session 变更监听回调失败", exc_info=True)

    @property
    def credential(self) -> Credential:
        return self.client.credential

    @property
    def logged_in(self) -> bool:
        return credential_has_login(self.client.credential)

    def require(self) -> Credential:
        cred = self.client.credential
        if not credential_has_login(cred):
            raise CredentialInvalidError("需要登录：请先在应用内扫码登录")
        return cred

    def adopt(self, credential: Credential) -> None:
        """登录成功后写入新凭证（内存 + 磁盘）."""
        with self._lock:
            self.client.credential = credential
            save_credential(credential)
            logger.info("登录凭证已更新 musicid=%s", credential.musicid)
            self._notify()

    async def logout(self) -> None:
        with self._lock:
            if self.logged_in:
                try:
                    await self.client.login.logout()
                except Exception:
                    logger.warning("上游登出失败，仅清除本地凭证", exc_info=True)
            clear_credential()
            self.client.credential = Credential()
            self._notify()


session = Session()


def self_euin() -> str:
    """当前账号的加密 UIN / 字符串 UIN（收藏歌单等接口的主键）."""
    cred = session.require()
    return cred.encrypt_uin or cred.str_musicid or str(cred.musicid)
