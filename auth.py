# -*- coding: utf-8 -*-
"""
登录模块：
  1. 微软正版登录（OAuth 设备码流程，浏览器输入代码即可，无需内嵌 WebView）
  2. 第三方登录（LittleSkin 等 Yggdrasil / authlib-injector 皮肤站）
账户统一存入 core 配置的 accounts 列表，type = microsoft / yggdrasil。
"""
from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any, Dict, Optional

import requests

import core

UA = core.UA

# 微软正版登录：使用 mcauth3（内置已通过 Minecraft 服务审批的公共客户端）。
# 高级用户可在设置里填自己的 Azure App ID（需加入 Mojang 允许列表）。
MS_TENANT = "consumers"
MS_SCOPE = "XboxLive.signin offline_access"
MS_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"

XBL_AUTH = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_AUTH = "https://xsts.auth.xboxlive.com/xsts/authorize"
MC_LOGIN = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_PROFILE = "https://api.minecraftservices.com/minecraft/profile"

LITTESKIN_API = "https://littleskin.cn/api/yggdrasil"


def _msa():
    """mcauth3 实例（用户配置了自定义 client_id 时用自定义）。"""
    import mcauth3
    cid = ""
    try:
        cid = (core.load().get("msClientId") or "").strip()
    except Exception:
        pass
    return mcauth3.MCMSA(client_id=cid) if cid else mcauth3.MCMSA()


def _post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str] | None = None,
               timeout: int = 20) -> Dict[str, Any]:
    r = requests.post(url, json=payload, headers={"User-Agent": UA,
                     "Accept": "application/json", **(headers or {})}, timeout=timeout)
    data = {}
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text[:200]}
    data["_status"] = r.status_code
    return data


def _post_form(url: str, data: Dict[str, str], timeout: int = 20) -> Dict[str, Any]:
    r = requests.post(url, data=data, headers={"User-Agent": UA,
                      "Content-Type": "application/x-www-form-urlencoded"}, timeout=timeout)
    out = {}
    try:
        out = r.json()
    except Exception:
        out = {"raw": r.text[:200]}
    out["_status"] = r.status_code
    return out


# ==================================================================
# 微软正版：设备码登录
# ==================================================================

def ms_device_code() -> Dict[str, Any]:
    """第一步：申请设备码。返回 user_code / verification_uri / interval 等。"""
    try:
        d = _msa().start_auth()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": f"获取设备码失败：{e}"}
    return {"ok": True, "deviceCode": d["device_code"], "userCode": d["user_code"],
            "verifyUrl": d.get("verification_uri", "https://www.microsoft.com/link"),
            "interval": int(d.get("interval") or 5), "expireIn": int(d.get("expires_in") or 900)}


def ms_device_poll(device_code: str) -> Dict[str, Any]:
    """
    第二步：轮询令牌（单次询问，pending 表示用户还没确认）。
    成功后走 mcauth3 的完整链路（XBL → XSTS → Minecraft → 档案）。
    """
    d = _post_form(MS_TOKEN_URL, {
        "client_id": _msa().client_id,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code})
    err = d.get("error")
    if err == "authorization_pending":
        return {"ok": False, "pending": True, "msg": "等待用户确认"}
    if err == "slow_down":
        return {"ok": False, "pending": True, "msg": "轮询过快，自动放慢"}
    if err:
        return {"ok": False, "msg": "登录失败：" + str(d.get("error_description") or err)}

    try:
        r = _msa()._exchange_microsoft_token(d["access_token"], d.get("refresh_token"))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": f"账户链路失败：{e}"}
    return _ms_result(r)


def _ms_result(r: Dict[str, Any]) -> Dict[str, Any]:
    """把 mcauth3 的返回结构转成启动器的账户。"""
    tokens = r.get("tokens") or {}
    prof = r.get("profile") or {}
    name = prof.get("name", "")
    uid = prof.get("id", "")
    if not name or not uid:
        return {"ok": False, "msg": "该账户没有 Minecraft 正版档案（需要购买 Java 版或订阅有效）"}
    return {
        "ok": True,
        "account": {
            "name": name,
            "uuid": _fmt_uuid(uid),
            "type": "microsoft",
            "accessToken": tokens.get("minecraft_access_token", ""),
            "refreshToken": tokens.get("microsoft_refresh_token", ""),
            "msTokenAt": int(time.time()),
            "loginAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    }


def _login_with_ms_token(ms_token: str, refresh_token: str = "") -> Dict[str, Any]:
    """MS 令牌 → XBL → XSTS → Minecraft → 档案（保留手动链路作备用）。"""
    xbl = _post_json(XBL_AUTH, {
        "Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com",
                       "RpsTicket": "d=" + ms_token},
        "RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT"})
    xbl_token = xbl.get("Token")
    if not xbl_token:
        return {"ok": False, "msg": "Xbox Live 认证失败：" + str(xbl.get("XErr") or xbl.get("raw") or "")[:120]}
    uhs = (((xbl.get("DisplayClaims") or {}).get("xui") or [{}])[0]).get("uhs", "")

    xsts = _post_json(XSTS_AUTH, {
        "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl_token]},
        "RelyingParty": "http://xboxlive.com", "TokenType": "JWT"})
    xsts_token = xsts.get("Token")
    if not xsts_token:
        xerr = xsts.get("XErr")
        why = {2148916233: "该微软账户没有 Xbox 档案，请先注册 Xbox",
               2148916238: "账户是未成年人，需要家长在 Xbox 设置里允许游玩"}.get(xerr, "")
        return {"ok": False, "msg": f"XSTS 认证失败（{xerr}）{('：' + why) if why else ''}"}
    uhs = (((xsts.get("DisplayClaims") or {}).get("xui") or [{}])[0]).get("uhs", "") or uhs

    mc = _post_json(MC_LOGIN, {"identityToken": f"XBL3.0 x={uhs};{xsts_token}"})
    mc_token = mc.get("access_token")
    if not mc_token:
        return {"ok": False, "msg": "Minecraft 登录失败：" + str(mc.get("error") or mc.get("details") or mc.get("raw") or "")[:120]}

    pr = requests.get(MC_PROFILE, headers={"User-Agent": UA, "Authorization": "Bearer " + mc_token}, timeout=20)
    if pr.status_code != 200:
        return {"ok": False, "msg": "该账户没有 Minecraft 正版档案（需要购买 Java 版或 Game Pass 订阅有效）"}
    prof = pr.json()
    if not prof.get("id"):
        return {"ok": False, "msg": "未取到玩家档案"}

    return {
        "ok": True,
        "account": {
            "name": prof.get("name", ""),
            "uuid": _fmt_uuid(prof.get("id", "")),
            "type": "microsoft",
            "accessToken": mc_token,
            "refreshToken": refresh_token,
            "msTokenAt": int(time.time()),
            "loginAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    }


def _fmt_uuid(s: str) -> str:
    s = (s or "").replace("-", "")
    if len(s) != 32:
        return s
    return "-".join([s[:8], s[8:12], s[12:16], s[16:20], s[20:]])


def ms_refresh(account: Dict[str, Any]) -> Dict[str, Any]:
    """用 refresh_token 续期微软账户令牌（mcauth3 全链路刷新）。"""
    rt = account.get("refreshToken") or ""
    if not rt:
        return {"ok": False, "msg": "该账户没有刷新令牌，请重新登录"}
    try:
        r = _msa().refresh_auth(rt)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": f"令牌续期失败，请重新登录（{e}）"}
    return _ms_result(r)


# ==================================================================
# 第三方（Yggdrasil / authlib-injector）：LittleSkin 及同协议皮肤站
# ==================================================================

def ygg_authenticate(server: str, username: str, password: str) -> Dict[str, Any]:
    """Yggdrasil authserver/authenticate。server 传 API 根（如 littleskin）。"""
    base = (server or LITTESKIN_API).rstrip("/")
    if base.endswith("/api/yggdrasil") or "/yggdrasil" not in base:
        pass
    d = _post_json(base + "/authserver/authenticate", {
        "agent": {"name": "Minecraft", "version": 1},
        "username": username, "password": password,
        "clientToken": core.offline_uuid("CuibingLauncher").replace("-", ""),
        "requestUser": True})
    if d.get("_status") != 200 or not d.get("accessToken"):
        return {"ok": False, "msg": "登录失败：" + str(d.get("errorMessage") or d.get("raw") or "用户名或密码错误")}
    profs = d.get("availableProfiles") or []
    sel = d.get("selectedProfile") or (profs[0] if profs else None)
    if not sel:
        return {"ok": False, "msg": "该账号下没有 Minecraft 角色"}
    return {
        "ok": True,
        "account": {
            "name": sel.get("name", ""),
            "uuid": _fmt_uuid(sel.get("id", "")),
            "type": "yggdrasil",
            "accessToken": d.get("accessToken", ""),
            "clientToken": d.get("clientToken", ""),
            "authServer": base,
            "home": username if "@" in username else "",
            "profiles": [{"id": p.get("id"), "name": p.get("name")} for p in profs],
            "loginAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    }


def ygg_validate(server: str, token: str, client_token: str = "") -> bool:
    base = (server or LITTESKIN_API).rstrip("/")
    try:
        r = requests.post(base + "/authserver/validate",
                          json={"accessToken": token, "clientToken": client_token} if client_token
                          else {"accessToken": token},
                          headers={"User-Agent": UA}, timeout=12)
        return r.status_code == 204
    except Exception:
        return False


def ygg_refresh(server: str, token: str, client_token: str = "") -> Dict[str, Any]:
    base = (server or LITTESKIN_API).rstrip("/")
    d = _post_json(base + "/authserver/refresh", {
        "accessToken": token,
        "clientToken": client_token,
        "requestUser": True})
    if not d.get("accessToken"):
        return {"ok": False, "msg": str(d.get("errorMessage") or "令牌已失效，请重新登录")}
    sel = d.get("selectedProfile") or {}
    return {"ok": True, "accessToken": d["accessToken"], "name": sel.get("name", "")}


def preflight_token(account: Dict[str, Any]) -> Dict[str, Any]:
    """
    启动前检查令牌：yggdrasil validate→refresh；microsoft 按需 refresh。
    返回 {"ok": bool, "account": 更新后的账户, "msg": str}
    """
    cfg = core.load()
    typ = account.get("type")
    if typ == "yggdrasil":
        server = account.get("authServer") or LITTESKIN_API
        tok = account.get("accessToken") or ""
        if ygg_validate(server, tok, account.get("clientToken") or ""):
            return {"ok": True, "account": account}
        r = ygg_refresh(server, tok, account.get("clientToken") or "")
        if not r.get("ok"):
            return {"ok": False, "msg": r.get("msg", "第三方令牌已失效，请重新登录")}
        account["accessToken"] = r["accessToken"]
        if r.get("name"):
            account["name"] = r["name"]
        _save_account(cfg, account)
        return {"ok": True, "account": account}
    if typ == "microsoft":
        # MC 令牌 24 小时有效，超过就刷新
        age = time.time() - int(account.get("msTokenAt") or 0)
        if age < 20 * 3600:
            return {"ok": True, "account": account}
        r = ms_refresh(account)
        if not r.get("ok"):
            return {"ok": False, "msg": r.get("msg", "正版令牌已过期，请重新登录")}
        acc = r["account"]
        acc["name"] = account.get("name") or acc["name"]
        _save_account(cfg, acc)
        return {"ok": True, "account": acc}
    return {"ok": True, "account": account}   # offline


def _save_account(cfg: Dict[str, Any], acc: Dict[str, Any]) -> None:
    accounts = cfg.get("accounts") or []
    hit = None
    for a in accounts:
        key = (a.get("type"), a.get("uuid") or a.get("name"))
        if key == (acc.get("type"), acc.get("uuid") or acc.get("name")):
            hit = a
            break
    if hit:
        hit.update(acc)
    else:
        accounts.append(acc)
    cfg["accounts"] = accounts
    cfg["activeAccount"] = acc.get("name") or cfg.get("activeAccount")
    core.save(cfg)


def add_logged_in(acc: Dict[str, Any]) -> Dict[str, Any]:
    """把登录成功的账户写入配置（同名离线账户会被替换升级）。"""
    cfg = core.load()
    accounts = cfg.get("accounts") or []
    # 同名离线账户 → 直接升级
    accounts = [a for a in accounts
                if not (a.get("name") == acc["name"] and a.get("type") == "offline")]
    hit = None
    for a in accounts:
        if a.get("uuid") == acc["uuid"] and a.get("type") == acc["type"]:
            hit = a
            break
    if hit:
        hit.update(acc)
    else:
        accounts.append(acc)
    cfg["accounts"] = accounts
    cfg["activeAccount"] = acc["name"]
    core.save(cfg)
    return {"ok": True, "account": acc}
