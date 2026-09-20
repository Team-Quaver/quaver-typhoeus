"""Quaver sidecar 主应用：FastAPI + 路由契约.

响应统一 {code:0,msg:"ok",data:<pydantic 序列化>}（对齐上游 QQMusicApi web 约定）。
错误经 exception handler 归一为 {code:-1,msg:...} + 对应 HTTP 状态码。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import random
import re
from collections.abc import Callable
from enum import Enum
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

import quaver_server  # noqa: F401  (触发 vendor path 注入)
from qqmusic_api import Credential
from qqmusic_api.core.exceptions import (
    BaseApiException,
    CredentialExpiredError,
    CredentialInvalidError,
    LoginError,
    RatelimitedError,
)
from qqmusic_api.models.login import QR
from qqmusic_api.modules.login import QRLoginType
from qqmusic_api.modules.song import SongFileInfo

from quaver_server.session import credential_has_login, credential_mode, self_euin, session
from quaver_server.streaming import resolve_stream, serve_stream, tiers_async
from typhoeus.errors import TyphoeusError

logger = logging.getLogger("quaver.app")


def ok(data: Any) -> dict[str, Any]:
    """标准成功信封；pydantic 模型与 dataclass 统一转 JSON 兼容结构."""
    if isinstance(data, BaseModel):
        payload = data.model_dump(mode="json", by_alias=False)
    elif isinstance(data, Enum):
        payload = data.value
    elif isinstance(data, list):
        payload = [_to_payload(x) for x in data]
    else:
        payload = data
    return {"code": 0, "msg": "ok", "data": payload}


def _to_payload(x: Any) -> Any:
    if isinstance(x, BaseModel):
        return x.model_dump(mode="json")
    return x


# —— 请求模型 ——
class SongUrlItem(BaseModel):
    mid: str
    file_type: int | None = None
    media_mid: str | None = None
    song_type: int | None = None


class SongUrlsBody(BaseModel):
    file_info: list[SongUrlItem]
    file_type: int = 13  # 128mp3（上游 web 层 SONG_FILE_TYPE_MAPPING 顺序值）


# file_type 整数 → SDK 枚举（与上游 web/src/modules/song.py 的 SONG_FILE_TYPES 顺序一致）
from qqmusic_api.modules.song import EncryptedSongFileType, SongFileType  # noqa: E402

FILE_TYPES = (
    SongFileType.DTS_X, SongFileType.MASTER, SongFileType.ATMOS_2, SongFileType.ATMOS_51,
    SongFileType.ATMOS_71, SongFileType.ATMOS_DB, SongFileType.NAC, SongFileType.FLAC,
    SongFileType.OGG_640, SongFileType.OGG_320, SongFileType.OGG_192, SongFileType.OGG_96,
    SongFileType.MP3_320, SongFileType.MP3_128, SongFileType.ACC_192, SongFileType.ACC_96,
    SongFileType.ACC_48,
    EncryptedSongFileType.DTS_X, EncryptedSongFileType.VINYL, EncryptedSongFileType.MASTER,
    EncryptedSongFileType.ATMOS_2, EncryptedSongFileType.ATMOS_51, EncryptedSongFileType.ATMOS_71,
    EncryptedSongFileType.ATMOS_DB, EncryptedSongFileType.NAC, EncryptedSongFileType.FLAC,
    EncryptedSongFileType.OGG_640, EncryptedSongFileType.OGG_320, EncryptedSongFileType.OGG_192,
    EncryptedSongFileType.OGG_96,
)


def file_type_of(code: int | None):
    if code is None:
        return SongFileType.MP3_128
    try:
        return FILE_TYPES[code]
    except IndexError as exc:
        raise HTTPException(422, f"未知 file_type: {code}") from exc


CDN_DOMAIN_FALLBACK = "https://isure.stream.qqmusic.qq.com/"
_cdn_domains: list[str] | None = None


async def cdn_domain() -> str:
    global _cdn_domains
    if _cdn_domains is None:
        try:
            dispatch = await session.client.song.get_cdn_dispatch()
            _cdn_domains = dispatch.sip or []
        except Exception:
            logger.warning("CDN dispatch 失败，回退默认域名", exc_info=True)
            _cdn_domains = []
    return random.choice(_cdn_domains) if _cdn_domains else CDN_DOMAIN_FALLBACK


async def call(fn: Callable[[], Any], *, need_login: bool = False) -> Any:
    """统一执行 SDK 调用：登录守卫 + 凭证过期自动刷新重试一次.

    注意凭证有效期：musickey 只有 3 天（credential.key_expires_in）。过期后上游
    只读接口照常可用、写接口一律返回「登录凭证已过期」（CredentialExpiredError，
    与 CredentialInvalidError 并非同一族，早先只 catch 后者 → 收藏/取消收藏会
    在一个静默窗口里全线失败）。所以这里既提前换新，也把两类异常都接住重试。
    """
    if need_login:
        cred = session.require()
        if cred.is_expired():
            await _try_refresh()
    try:
        return await fn()
    except (CredentialInvalidError, CredentialExpiredError):
        if not session.logged_in:
            raise
        refreshed = await _try_refresh()
        if refreshed is None:
            raise
        return await fn()


async def _try_refresh() -> Credential | None:
    try:
        cred = await session.client.login.refresh_credential()
        session.adopt(cred)
        logger.info("凭证已自动刷新 musicid=%s", cred.musicid)
        return cred
    except Exception:
        logger.warning("凭证刷新失败", exc_info=True)
        return None


def _serialize_qr(qr: QR) -> dict[str, Any]:
    data = base64.b64encode(qr.data).decode("ascii") if qr.data else ""
    return {
        "qr_type": qr.qr_type.value,
        "identifier": qr.identifier,
        "mimetype": qr.mimetype,
        "data": data,
        "img": f"data:{qr.mimetype};base64,{data}" if data else "",
    }


# QR 事件 → 数字（与上游 web 层一致）
QR_EVENTS = {"DONE": 0, "SCAN": 1, "CONF": 2, "TIMEOUT": 3, "REFUSE": 4}

app = FastAPI(title="Quaver API sidecar", version="0.1.0", docs_url="/swagger", redoc_url=None)


@app.exception_handler(BaseApiException)
async def _api_exc(_r: Request, exc: BaseApiException) -> JSONResponse:
    if isinstance(exc, RatelimitedError):
        status = 429
    elif isinstance(exc, (CredentialInvalidError,)):
        status = 401
    elif isinstance(exc, LoginError):
        status = 400
    else:
        status = 400
    return JSONResponse(status_code=status, content={"code": -1, "msg": str(exc)})


@app.exception_handler(StarletteHTTPException)
async def _http_exc(_r: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"code": -1, "msg": str(exc.detail)})


@app.exception_handler(TyphoeusError)
async def _typhoeus_exc(_r: Request, exc: TyphoeusError) -> JSONResponse:
    # Typhoeus 错误族自带 status（403 会员门控 / 451 加密档拒绝 / 502 provider / 422 未知档）
    return JSONResponse(status_code=exc.status, content={"code": -exc.status, "msg": str(exc)})


@app.exception_handler(RequestValidationError)
async def _val_exc(_r: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"code": -1, "msg": "请求参数校验失败"})


# ===================== 登录 =====================

QR_TYPES = {"qq": QRLoginType.QQ, "wx": QRLoginType.WX, "mobile": QRLoginType.MOBILE}
_qr_locks: dict[str, asyncio.Lock] = {}
# mobile 型走 MQTT 推送：GET 二维码时启动后台消费任务，状态从这里读
_mobile_states: dict[str, dict[str, Any]] = {}


def _qr_lock(identifier: str) -> asyncio.Lock:
    lock = _qr_locks.get(identifier)
    if lock is None:
        lock = _qr_locks[identifier] = asyncio.Lock()
    return lock


@app.get("/login/qrcode/{login_type}")
async def login_qrcode(login_type: str):
    t = QR_TYPES.get(login_type)
    if t is None:
        raise HTTPException(422, f"不支持的登录类型: {login_type}（可选 qq/wx/mobile）")
    qr = await session.client.login.get_qrcode(t)
    if t is QRLoginType.MOBILE:
        _mobile_states[qr.identifier] = {"event": 1, "done": False, "credential": None}
        asyncio.get_running_loop().create_task(_consume_mobile_qr(qr))
    return ok(_serialize_qr(qr))


async def _consume_mobile_qr(qr: QR) -> None:
    """后台消费手机客户端扫码事件流（MQTT 生命周期内持续推送）."""
    state = _mobile_states.get(qr.identifier)
    try:
        async for result in session.client.login.checking_mobile_qrcode(qr, deadline=None):
            if state is None:
                return
            state["event"] = QR_EVENTS.get(result.event.name, -1)
            state["done"] = result.done
            if result.done and result.credential is not None:
                session.adopt(result.credential)
                state["credential"] = jsonable_credential(result.credential)
                return
            if result.event.name in ("TIMEOUT", "REFUSE"):
                return
    except Exception as exc:
        logger.warning("mobile 二维码事件流异常: %s", exc)
        if state is not None:
            state["event"] = -1
            state["done"] = True
            state["error"] = str(exc)


@app.get("/login/qrcode/{login_type}/status")
async def login_qrcode_status(login_type: str, identifier: str):
    t = QR_TYPES.get(login_type)
    if t is None:
        raise HTTPException(422, f"不支持的登录类型: {login_type}")
    if t is QRLoginType.MOBILE:
        state = _mobile_states.get(identifier)
        if state is None:
            return ok({"event": 3, "done": True, "credential": None, "error": "二维码不存在或已过期"})
        payload = dict(state)
        payload.setdefault("identifier", identifier)
        return ok(payload)
    qr = QR(data=b"", qr_type=t, mimetype="image/png", identifier=identifier)
    async with _qr_lock(identifier):
        try:
            result = await session.client.login.check_qrcode(qr)
        except LoginError as exc:
            # 上游把 104401(过期)/20450(封号) 等包成 LoginError；对 UI 来说过期/拒绝仍应可继续流程
            return ok({"event": QR_EVENTS.get(_guess_login_event(exc), 3), "done": True, "credential": None,
                       "error": str(exc)})
    payload: dict[str, Any] = {
        "event": QR_EVENTS.get(result.event.name, -1),
        "done": result.done,
        "identifier": identifier,
        "login_type": login_type,
    }
    if result.done and result.credential is not None:
        session.adopt(result.credential)
        payload["credential"] = jsonable_credential(result.credential)
    return ok(payload)


def _guess_login_event(exc: LoginError) -> str:
    s = str(exc)
    if "104401" in s or "104400" in s or "过期" in s or "expired" in s.lower():
        return "TIMEOUT"
    if "20450" in s or "封" in s or "受限" in s:
        return "REFUSE"
    return "TIMEOUT"


def jsonable_credential(c: Credential) -> dict[str, Any]:
    """给 UI 的凭证摘要——绝不下发 musickey/refresh_token 等敏感字段."""
    return {
        "musicid": c.musicid,
        "str_musicid": c.str_musicid,
        "encrypt_uin": c.encrypt_uin,
        "login_type": c.login_type,
        "expired_at": c.expired_at,
        "key_expires_in": c.key_expires_in,
    }


@app.get("/login/status")
async def login_status():
    cred = session.credential
    logged_in = credential_has_login(cred)
    expired = logged_in and cred.is_expired()
    # credential_mode：external=凭证交给 Electron 主进程（系统密钥管理器加密）；memory=只驻内存、
    # 关掉即需重新登录。只报模式（不报后端名 —— 那是主进程侧才知道的事），便于手工 curl 确认。
    return ok({"logged_in": logged_in, "expired": expired, "credential_mode": credential_mode(),
               "credential": jsonable_credential(cred) if logged_in else None})


@app.get("/login/refresh")
async def login_refresh():
    cred = session.require()
    refreshed = await session.client.login.refresh_credential(cred)
    session.adopt(refreshed)
    return ok(jsonable_credential(refreshed))


@app.post("/login/logout")
async def login_logout():
    await session.logout()
    return ok({"logged_out": True})


# ===================== 用户 =====================

@app.get("/user/me")
async def user_me():
    """当前账号主页信息（昵称/头像/加密 UIN/音乐人标志）.

    SDK 的 UserHomepageResponse 模型 extra="ignore"，把原始 BaseInfo 里的
    IsSinger（腾讯音乐人认证标志）丢掉了——这里透传原始响应并挑出所需字段，
    不改 vendor 子模块。
    """
    euin = self_euin()
    user_mod = session.client.user
    raw = await call(lambda: user_mod._build_cgi(
        module="music.UnifiedHomepage.UnifiedHomepageSrv",
        method="GetHomepageHeader",
        param={"uin": euin, "IsQueryTabDetail": 1},
        disable_parse=True,
        credential=user_mod._resolve_placeholder_credential(None),
    ))
    base = ((raw or {}).get("Info") or {}).get("BaseInfo") or {}
    return ok({
        "base_info": {
            "name": base.get("Name", ""),
            "avatar": base.get("Avatar", ""),
            "encrypted_uin": base.get("EncryptedUin", ""),
            "user_type": base.get("UserType", 0),
            "is_singer": bool(base.get("IsSinger", 0)),
        }
    })


@app.get("/user/vip")
async def user_vip():
    return ok(await call(lambda: session.client.user.get_vip_info(), need_login=True))


@app.get("/user/liked")
async def user_liked(page: int = 1, num: int = 30):
    euin = self_euin()
    return ok(await call(lambda: session.client.user.get_fav_song(euin, page=page, num=num), need_login=True))


@app.get("/user/created-songlists")
async def user_created_songlists():
    cred = session.require()
    uin = cred.musicid
    return ok(await call(lambda: session.client.user.get_created_songlist(uin)))


@app.get("/user/fav-songlists")
async def user_fav_songlists(page: int = 1, num: int = 20):
    euin = self_euin()
    return ok(await call(lambda: session.client.user.get_fav_songlist(euin, page=page, num=num)))


# ===================== 播放流（Typhoeus：高档位协商 + 会员门控 + Range 中继） =====================

class StreamResolveBody(BaseModel):
    mid: str
    media_mid: str | None = None
    song_type: int = 0
    tier: str = "128"          # typhoeus.quality.TierId
    auto: bool = False         # true=自动音质模式（会员不足向下降档，不报 403）
    deprioritize: list[str] = []  # 回退链降权档位（UI「回退排序」开关：如 atmos51 压到链尾）


@app.get("/stream/tiers")
async def stream_tiers():
    """当前账号可播档位（会员门控数据源；加密档位永不出现——本后端不解密）。"""
    return ok(await tiers_async())


@app.post("/stream/resolve")
async def stream_resolve(body: StreamResolveBody):
    """协商一档明文流并换取中继 token；会员不足回 403，加密档回 451。"""
    return ok(await resolve_stream(body.mid, body.media_mid, body.tier, body.auto, body.deprioritize))


@app.get("/stream/{token}")
async def stream_proxy(token: str, request: Request):
    """明文流 Range 中继（<audio> src 指这里；vkey 留在后端，浏览器只见 token）。"""
    return await serve_stream(token, request)


# ===================== 歌曲 =====================

@app.post("/song/urls")
async def song_urls(body: SongUrlsBody):
    """批量取播放链接；返回 {expiration, items:[{mid,url,result,...}]}（url 已拼 CDN）.

    注意：per-item file_type 未显式给出时必须传 None——SDK 里 `item.file_type or file_type`
    会优先取 item 的；若像早期版本那样默认成 MP3_128，批量档位会被静默覆盖成 128kbps。
    """
    resp = await call(lambda: session.client.song.get_song_urls(
        [SongFileInfo(mid=i.mid, file_type=file_type_of(i.file_type) if i.file_type is not None else None,
                      media_mid=i.media_mid, song_type=i.song_type) for i in body.file_info],
        file_type=file_type_of(body.file_type),
    ))
    domain = await cdn_domain()
    items = [i.model_dump(mode="json") | {"url": (domain + i.purl) if i.purl else ""} for i in resp.data]
    return ok({"expiration": resp.expiration, "items": items})


@app.get("/song/{value}/detail")
async def song_detail(value: str):
    return ok(await call(lambda: session.client.song.get_detail(int(value) if value.isdigit() else value)))


@app.get("/song/{value}/url")
async def song_url_single(value: str, file_type: int = 13, media_mid: str | None = None):
    resp = await call(lambda: session.client.song.get_song_urls(
        [SongFileInfo(mid=value, media_mid=media_mid)], file_type=file_type_of(file_type)))
    domain = await cdn_domain()
    items = [i.model_dump(mode="json") | {"url": (domain + i.purl) if i.purl else ""} for i in resp.data]
    return ok({"expiration": resp.expiration, "items": items})


# ===================== 歌单 =====================

@app.get("/songlist/{songlist_id}/detail")
async def songlist_detail(songlist_id: int, page: int = 1, num: int = 100):
    return ok(await call(lambda: session.client.songlist.get_detail(
        songlist_id, num=num, page=page, onlysong=False)))


@app.post("/songlist/{songlist_id}/like")
async def songlist_like(songlist_id: int):
    return ok(await call(lambda: session.client.user.fav_songlist(songlist_id), need_login=True))


@app.delete("/songlist/{songlist_id}/like")
async def songlist_unlike(songlist_id: int):
    return ok(await call(lambda: session.client.user.unfav_songlist(songlist_id), need_login=True))


@app.get("/songlist/fav/check")
async def songlist_fav_check():
    """已收藏的他人公开歌单 ID 集合（收藏态徽章数据源）."""
    resp = await call(lambda: session.client.user.get_fav_songlist(self_euin(), page=1, num=100))
    return ok({"ids": [s.id for s in resp.playlists]})


class SonglistSongBody(BaseModel):
    """歌单写操作的歌曲引用.

    song_type 必须用**写侧**枚举（读侧 Song.type - 1，见 ui/src/lib/api.ts:writeSongType）：
    发读侧原值时上游 retCode=0 但什么都不会发生（静默空操作）。
    tid 是歌单的 tid（= 创建/收藏列表里的 id，不是 dirid）；上游多数情况容得下 0，
    但自建歌单带上真实 tid 更稳 —— 调用方知道就传。
    """

    song_id: int
    song_type: int = 0
    tid: int = 0


@app.post("/songlist/{dirid}/songs")
async def songlist_add_song(dirid: int, body: SonglistSongBody):
    """把歌曲加入指定歌单（PlaylistDetailWrite AddSonglist，仅自建歌单可写）."""
    added = await call(
        lambda: session.client.songlist.add_songs(dirid, [(body.song_id, body.song_type)], tid=body.tid),
        need_login=True,
    )
    return ok({"ok": bool(added)})


@app.delete("/songlist/{dirid}/songs")
async def songlist_del_song(dirid: int, body: SonglistSongBody):
    """把歌曲从指定歌单移除（PlaylistDetailWrite DelSonglist）.

    注意 dirid ≠ disstid：歌单详情里的 info.dirid 才是这里要的目录 ID，
    而 info.id 是 tid（两者都通过 GET /songlist/{id}/detail 拿到）。
    """
    removed = await call(
        lambda: session.client.songlist.del_songs(dirid, [(body.song_id, body.song_type)], tid=body.tid),
        need_login=True,
    )
    return ok({"ok": bool(removed)})


class SongLikeBody(BaseModel):
    song_id: int
    song_type: int = 0


@app.post("/song/like")
async def song_like(body: SongLikeBody):
    return ok(await call(lambda: session.client.songlist.like_song(
        [(body.song_id, body.song_type)]), need_login=True))


@app.post("/song/unlike")
async def song_unlike(body: SongLikeBody):
    return ok(await call(lambda: session.client.songlist.unlike_song(
        [(body.song_id, body.song_type)]), need_login=True))


# ===================== 歌词 =====================

@app.get("/song/{value}/lyric")
async def song_lyric(value: str, trans: bool = False, roma: bool = False, qrc: bool = False):
    # 注意：/song/{value}/lyric 与 /song/{value}/detail 共享前缀；FastAPI 按注册顺序匹配，无冲突
    return ok(await call(lambda: session.client.lyric.get_lyric(
        int(value) if value.isdigit() else value, trans=trans, roma=roma, qrc=qrc)))


# ===================== 搜索 =====================

@app.get("/search/hotkey")
async def search_hotkey():
    return ok(await call(session.client.search.get_hotkey))


@app.get("/search/complete")
async def search_complete(keyword: str):
    return ok(await call(lambda: session.client.search.complete(keyword)))


@app.get("/search")
async def search(keyword: str, type: int = 0, page: int = 1, num: int = 20):
    return ok(await call(lambda: session.client.search.search_by_type(
        keyword, search_type=type, page=page, num=num)))


@app.get("/search/general")
async def search_general(keyword: str, page: int = 1, num: int = 15):
    return ok(await call(lambda: session.client.search.general_search(keyword, page=page, num=num)))


# ===================== 推荐 / 榜单 =====================

@app.get("/recommend/guess")
async def recommend_guess(rounds: int = 6):
    """猜你喜欢（无限电台那一路），一次凑够 rounds×5 首。

    上游 `music.radioProxy.MbTrackRadioSvr/get_radio_track` **只给 5 首**：
    `num` 调大被忽略（实测 num=5/10/20/50 都回 5 首）、回灌 `song_ids` 续拿直接报 22006。
    好消息是**每次调用内容随机**，所以「多拿些」= 多调几轮再按 mid 去重。

    ⚠️ **必须串行**：并发同样的请求上游只放行一个，其余全回 700000
    （实测并发 2/3/4/6 轮都只有 1 轮成功；串行 4 轮即使 gap=0 也 4/4 成功）。
    所以这里不图那一个 RTT，老实一轮一轮来（每轮 ~300ms，6 轮 ≈ 2s，前端有加载态）。
    单轮失败不算整体失败（保底返回已拿到的），全失败才抛。
    """
    rounds = max(1, min(rounds, 8))
    songs: list[Any] = []
    seen: set[str] = set()
    for i in range(rounds):
        try:
            r: Any = await call(lambda: session.client.recommend.get_guess_recommend())
        except Exception as exc:  # noqa: BLE001 — 单轮失败（含 700000 节流）不该拖垮整页
            logger.warning("猜你喜欢第 %d/%d 轮失败：%s", i + 1, rounds, exc)
            continue
        for s in getattr(r, "songs", None) or []:
            mid = getattr(s, "mid", "")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            songs.append(s)
    if not songs:
        raise HTTPException(502, "猜你喜欢上游无响应，请稍后重试")
    return ok({"songs": [_to_payload(s) for s in songs], "rounds": rounds})


@app.get("/recommend/songlist")
async def recommend_songlist(page: int = 1, num: int = 25):
    return ok(await call(lambda: session.client.recommend.get_recommend_songlist(page=page, num=num)))


@app.get("/recommend/newsong")
async def recommend_newsong(type: int = 5):
    return ok(await call(lambda: session.client.recommend.get_recommend_newsong(type=type)))


# 「每日30首」= 系统虚拟歌单，**dirid 固定 202**（与「我喜欢」= 201 同一族）：
# 它每天由服务端重生成一份 30 首的个性化歌单，disstid（本例 7083466040）**每天都变**
# —— 实测 created-songlists 里也不列它，首页 feed 的卡片虽然带 id/dirid，但按 dirid 取才稳。
# 读法与普通歌单详情完全一致（CgiGetDiss），只是 disstid 与 dirid 都传 202：
# get_detail(202, dirid=202) → info.title「<昵称>的每日30首」、info.dirid=202、total=30。
DAILY_DIRID = 202


@app.get("/recommend/daily")
async def recommend_daily(page: int = 1, num: int = 50):
    """每日30首（系统 dirid=202；返回结构与 /songlist/{id}/detail 一致）."""
    return ok(await call(lambda: session.client.songlist.get_detail(
        DAILY_DIRID, dirid=DAILY_DIRID, num=num, page=page, onlysong=False)))


@app.get("/top/category")
async def top_category():
    return ok(await call(session.client.top.get_category))


@app.get("/top/{top_id}/detail")
async def top_detail(top_id: int, page: int = 1, num: int = 50):
    return ok(await call(lambda: session.client.top.get_detail(top_id, page=page, num=num)))


# ===================== 专辑 / 歌手 =====================

@app.get("/album/{value}/detail")
async def album_detail(value: str):
    return ok(await call(lambda: session.client.album.get_detail(int(value) if value.isdigit() else value)))


@app.get("/album/{value}/songs")
async def album_songs(value: str, page: int = 1, num: int = 50):
    return ok(await call(lambda: session.client.album.get_song(
        int(value) if value.isdigit() else value, page=page, num=num)))


@app.get("/singer/{mid}/info")
async def singer_info(mid: str):
    return ok(await call(lambda: session.client.singer.get_info(mid)))


@app.get("/singer/{mid}/songs")
async def singer_songs(mid: str, page: int = 1, num: int = 50, order: int = 1):
    """歌手歌曲列表：order 1=按热度（热门）、2=按发行时间倒序（最新发布）。

    order 必须显式透传：上游 SDK 默认 order=1，前端「新歌」标签整页靠 order=2 取最新发行，
    漏传时两次请求拿到同一批热门歌，前端按 mid 去重后新歌面板恒为空（「无法获取歌手新歌」）。
    """
    return ok(await call(lambda: session.client.singer.get_songs_list(
        mid, page=page, num=num, order=order)))


@app.get("/singer/{mid}/albums")
async def singer_albums(mid: str, page: int = 1, num: int = 30):
    return ok(await call(lambda: session.client.singer.get_album_list(mid, page=page, num=num)))


@app.get("/singer/{mid}/similar")
async def singer_similar(mid: str, number: int = 10):
    return ok(await call(lambda: session.client.singer.get_similar(mid, number=number)))


# ex_info.area 是上游的「地区枚举 id」（原始 int，不是可读文案）：实测 0=港台、1=内地、
# 2=日韩、3=欧美、5=其他（1=周深/薛之谦、2=米津玄師/BTS、3=Coldplay/Taylor Swift、5=其他语种）。
# 直接透传的话，歌手页信息头（地区位）会漏出裸数字——欧美歌手莫名显示成「3」。
SINGER_AREA_LABELS = {0: "港台", 1: "内地", 2: "日韩", 3: "欧美", 5: "其他"}


def singer_area_label(raw: Any) -> str:
    """地区枚举 id → 展示文案；未知取值一律返回空字符串（宁可不出，也不漏裸数字）."""
    try:
        return SINGER_AREA_LABELS.get(int(str(raw).strip() or 0), "")
    except (TypeError, ValueError):
        return ""


@app.get("/singer/{mid}/desc")
async def singer_desc(mid: str):
    """歌手详情归一化：百科简介（wiki <desc> 优先，回退 ex_info.desc）+ 外文名/生日/地区/立绘。"""
    d = await call(lambda: session.client.singer.get_desc([mid]))
    if not d.singer_list:
        return ok(None)
    s = d.singer_list[0]
    m = re.search(r"<desc><!\[CDATA\[(.*?)\]\]></desc>", s.wiki or "", re.S)
    desc = (m.group(1) if m else s.ex_info.desc).strip()
    return ok({
        "name": s.basic_info.name,
        "mid": s.basic_info.mid,
        "pic": s.pic.pic,
        "big_pic": s.pic.big_black,
        "desc": desc,
        "foreign_name": s.ex_info.foreign_name,
        "birthday": s.ex_info.birthday,
        # 注：area 经 NoneOrZeroToEmptyStr 规整，港台歌手上游给 0，到这里是 ""（视作 0 处理）
        "area": singer_area_label(s.ex_info.area),
        "identity": s.ex_info.identity,
    })


# ===================== 评论（二期入口占位，先接通热评） =====================

@app.get("/song/{song_id}/comments")
async def song_comments(song_id: int, page: int = 1, num: int = 20):
    return ok(await call(lambda: session.client.comment.get_hot_comments(
        song_id, page_num=page, page_size=num)))


# ===================== 健康检查 =====================

@app.get("/")
async def root():
    return {"code": 0, "msg": "quaver-sidecar ok", "backend": "L-1124/QQMusicApi"}


def create_app() -> FastAPI:  # uvicorn factory 入口
    return app
