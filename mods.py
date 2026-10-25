# -*- coding: utf-8 -*-
"""
脆冰启动器 - 资源平台
Modrinth / CurseForge / 红石中继站 三源搜索与下载。
支持 mod、资源包、光影包、整合包，安装到指定版本（受版本隔离影响）。
"""
from __future__ import annotations

import json
import os
import time
import zipfile
from typing import Any, Dict, List, Optional, Tuple

import requests

import core
import version as vmod

UA = core.UA

# 平台标识
SOURCES = {
    "modrinth": {"name": "Modrinth", "home": "https://modrinth.com"},
    "curseforge": {"name": "CurseForge", "home": "https://www.curseforge.com"},
    "mczwlt": {"name": "红石中继站", "home": "https://www.mczwlt.net"},
}
SOURCE_ORDER = ["modrinth", "curseforge", "mczwlt"]

# 资源类型：-> (Modrinth project_type, CurseForge classId, 子目录)
KINDS = {
    "mod": {"name": "模组", "mr": "mod", "cf": 6, "dir": "mods"},
    "resourcepack": {"name": "资源包", "mr": "resourcepack", "cf": 12, "dir": "resourcepacks"},
    "shader": {"name": "光影", "mr": "shader", "cf": 4546, "dir": "shaderpacks"},
    "modpack": {"name": "整合包", "mr": "modpack", "cf": 4471, "dir": ""},
}

MODRINTH = "https://api.modrinth.com/v2"
MODRINTH_MIRROR = "https://mod.mcimirror.top/modrinth/v2"
CURSEFORGE = "https://api.curseforge.com/v1"
CURSEFORGE_MIRROR = "https://mod.mcimirror.top/curseforge/v1"
MCZWLT = "https://www.mczwlt.net"

CF_GAME_ID = 432          # Minecraft


def _mirror(cfg: Dict[str, Any]) -> bool:
    return bool(cfg.get("resourceMirror", True))


def mr_base(cfg: Dict[str, Any]) -> str:
    return MODRINTH_MIRROR if _mirror(cfg) else MODRINTH


def cf_base(cfg: Dict[str, Any]) -> str:
    return CURSEFORGE_MIRROR if _mirror(cfg) else CURSEFORGE


def _get(url: str, headers: Optional[Dict[str, str]] = None,
         timeout: Tuple[int, int] = (6, 20)) -> Any:
    h = {"User-Agent": UA, "Connection": "close"}
    h.update(headers or {})
    for attempt in range(2):
        try:
            r = requests.get(url, timeout=timeout, headers=h)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (401, 403):
                raise RuntimeError(f"平台拒绝了请求（HTTP {r.status_code}）")
        except RuntimeError:
            raise
        except Exception:
            time.sleep(0.35)
    return None


def resolve_sources(choice: str) -> List[str]:
    """auto -> 按优先级尝试；手动 -> 只用指定源。"""
    if choice and choice != "auto":
        return [choice]
    return list(SOURCE_ORDER)


# ---------------- 搜索 ----------------

def _norm_mr(p: Dict[str, Any]) -> Dict[str, Any]:
    vers = p.get("versions") or []
    mc_max = ""
    if vers:
        try:
            mc_max = max(vers, key=_ver_key)
        except Exception:
            mc_max = vers[-1]
    return {
        "source": "modrinth",
        "id": p.get("project_id") or p.get("id") or p.get("slug"),
        "slug": p.get("slug", ""),
        "name": p.get("title") or p.get("name", ""),
        "summary": (p.get("description") or "")[:160],
        "icon": p.get("icon_url") or "",
        "downloads": p.get("downloads") or 0,
        "follows": p.get("follows") or 0,
        "author": p.get("author") or "",
        "kind": p.get("project_type") or (p.get("all_project_types") or ["mod"])[0],
        "categories": p.get("categories") or p.get("display_categories") or [],
        "updated": (p.get("date_modified") or "")[:19].replace("T", " "),
        "mcMax": mc_max,
        "mcCount": len(vers),
        "link": "https://modrinth.com/" + (p.get("project_type") or "mod") + "/" + (p.get("slug") or ""),
    }


def _norm_cf(p: Dict[str, Any], kind: str) -> Dict[str, Any]:
    logo = (p.get("logo") or {}).get("thumbnailUrl") or (p.get("logo") or {}).get("url") or ""
    vers: List[str] = []
    for fi in p.get("latestFilesIndexes") or []:
        gv = fi.get("gameVersion")
        if gv and gv not in vers:
            vers.append(gv)
    mc_max = ""
    if vers:
        try:
            mc_max = max(vers, key=_ver_key)
        except Exception:
            mc_max = vers[-1]
    return {
        "source": "curseforge",
        "id": str(p.get("id")),
        "slug": p.get("slug", ""),
        "name": p.get("name", ""),
        "summary": (p.get("summary") or "")[:160],
        "icon": logo,
        "downloads": p.get("downloadCount") or 0,
        "follows": p.get("thumbsUpCount") or 0,
        "author": (p.get("authors") or [{}])[0].get("name", ""),
        "kind": kind,
        "categories": [c.get("name") for c in (p.get("categories") or [])][:5],
        "updated": (p.get("fileDate") or p.get("dateModified") or "")[:19].replace("T", " "),
        "mcMax": mc_max,
        "mcCount": len(vers),
        "link": p.get("links", {}).get("websiteUrl") or "",
    }


def search_modrinth(query: str, kind: str, mc: str, loader: str,
                    limit: int, cfg: Dict[str, Any], index: str = "downloads") -> List[Dict[str, Any]]:
    import urllib.parse
    facets: List[List[str]] = [["project_type:" + KINDS[kind]["mr"]]]
    if mc:
        facets.append(["versions:" + mc])
    if loader:
        facets.append(["categories:" + loader])
    params = {
        "query": query,
        "limit": str(limit),
        "index": index,
        "facets": json.dumps(facets, separators=(",", ":")),
    }
    url = mr_base(cfg) + "/search?" + urllib.parse.urlencode(params)
    data = _get(url)
    if not data or "hits" not in data:
        return []
    return [_norm_mr(h) for h in data["hits"]]


def search_curseforge(query: str, kind: str, mc: str, loader: str,
                      limit: int, cfg: Dict[str, Any], sort_field: str = "6") -> List[Dict[str, Any]]:
    import urllib.parse
    headers = {}
    key = (cfg.get("curseforgeKey") or "").strip()
    if key:
        headers["x-api-key"] = key
    params = {
        "gameId": str(CF_GAME_ID),
        "classId": str(KINDS[kind]["cf"]),
        "searchFilter": query,
        "pageSize": str(limit),
        "sortField": sort_field,   # 6=下载量 2=最近更新
        "sortOrder": "desc",
    }
    if mc:
        params["gameVersion"] = mc
    if loader:
        params["modLoaderType"] = {"fabric": "4", "forge": "1",
                                   "neoforge": "6", "quilt": "5"}.get(loader, "")
    if not params.get("modLoaderType"):
        params.pop("modLoaderType", None)
    url = cf_base(cfg) + "/mods/search?" + urllib.parse.urlencode(params)
    data = _get(url, headers=headers)
    if not data or "data" not in data:
        return []
    return [_norm_cf(p, kind) for p in data["data"]]


def search_mczwlt(query: str, kind: str, mc: str, loader: str,
                  limit: int, cfg: Dict[str, Any], index: str = "downloads") -> List[Dict[str, Any]]:
    """
    红石中继站目前没有开放的搜索接口（站点搜索是纯前端渲染）。
    它以 Modrinth / CurseForge 为数据源做聚合，因此这里沿用 Modrinth 数据，
    并为每条结果补上红石中继站的检索入口。
    """
    base = search_modrinth(query, kind, mc, loader, limit, cfg, index=index)
    import urllib.parse
    for it in base:
        it["source"] = "mczwlt"
        it["origin"] = "modrinth"
        it["link"] = MCZWLT + "/search?keyword=" + urllib.parse.quote(it.get("name", ""))
    return base


def search(query: str, kind: str = "mod", source: str = "auto", mc: str = "",
           loader: str = "", limit: int = 24,
           sort: str = "downloads", order: str = "desc") -> Dict[str, Any]:
    cfg = core.load()
    choice = source or cfg.get("resourceSource", "auto") or "auto"
    results: List[Dict[str, Any]] = []
    tried: List[str] = []
    errors: Dict[str, str] = {}
    used = choice

    def _run(src: str, mc_v: str, ld_v: str) -> List[Dict[str, Any]]:
        if src == "modrinth":
            return search_modrinth(query, kind, mc_v, ld_v, limit, cfg,
                                   index="updated" if sort == "updated" else "downloads")
        if src == "curseforge":
            return search_curseforge(query, kind, mc_v, ld_v, limit, cfg,
                                     sort_field="2" if sort == "updated" else "6")
        return search_mczwlt(query, kind, mc_v, ld_v, limit, cfg,
                             index="updated" if sort == "updated" else "downloads")

    relaxed = False
    for src in resolve_sources(choice):
        tried.append(src)
        try:
            results = _run(src, mc, loader)
        except Exception as e:  # noqa: BLE001
            errors[src] = str(e)[:140]
            results = []
        # 版本/加载器筛选过严导致空结果时，放宽条件重试一次
        if not results and (mc or loader):
            try:
                loose = _run(src, "", "")
            except Exception:  # noqa: BLE001
                loose = []
            if loose:
                results = loose
                relaxed = True
        if results:
            used = src
            break

    results = _sort_results(results, sort, order)

    note = ""
    if used == "mczwlt":
        note = "红石中继站暂无开放搜索接口，当前结果由 Modrinth 数据聚合，点击卡片可在站内查看"
    if relaxed:
        tail = "按当前版本/加载器筛选后没有结果，已放宽条件展示；安装前请留意兼容性提示"
        note = (note + "；" if note else "") + tail
    if not (query or "").strip():
        pre = "未输入关键词，正在浏览热门资源"
        note = (pre + "；" + note) if note else pre

    return {
        "query": query, "kind": kind, "source": used, "results": results,
        "tried": tried, "errors": errors, "relaxed": relaxed, "note": note,
    }


def _sort_results(results: List[Dict[str, Any]], sort: str, order: str) -> List[Dict[str, Any]]:
    """按下载量 / 更新时间 / 支持版本正倒序。"""
    rev = (order or "desc") != "asc"
    if sort == "updated":
        return sorted(results, key=lambda p: p.get("updated") or "", reverse=rev)
    if sort == "versions":
        def k(p):
            try:
                return _ver_key(p.get("mcMax") or "0.0")
            except Exception:
                return _ver_key("0.0")
        return sorted(results, key=k, reverse=rev)
    # 默认下载量
    return sorted(results, key=lambda p: p.get("downloads") or 0, reverse=rev)


# ---------------- 版本（文件）列表 ----------------

def versions_modrinth(pid: str, mc: str, loader: str, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    import urllib.parse
    params = {}
    if mc:
        params["game_versions"] = json.dumps([mc])
    if loader:
        params["loaders"] = json.dumps([loader])
    url = mr_base(cfg) + f"/project/{pid}/version"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = _get(url)
    out = []
    for v in data or []:
        files = v.get("files") or []
        primary = next((f for f in files if f.get("primary")), files[0] if files else None)
        if not primary:
            continue
        out.append({
            "id": v.get("id"),
            "name": v.get("name") or v.get("version_number"),
            "version": v.get("version_number", ""),
            "mc": v.get("game_versions") or [],
            "loaders": v.get("loaders") or [],
            "url": primary.get("url"),
            "fileName": primary.get("filename"),
            "size": primary.get("size", 0),
            "date": (v.get("date_published") or "")[:10],
            "channel": v.get("version_type", ""),
        })
    return out


def versions_curseforge(pid: str, mc: str, loader: str, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    import urllib.parse
    headers = {}
    key = (cfg.get("curseforgeKey") or "").strip()
    if key:
        headers["x-api-key"] = key
    params = {"pageSize": "50"}
    if mc:
        params["gameVersion"] = mc
    url = cf_base(cfg) + f"/mods/{pid}/files?" + urllib.parse.urlencode(params)
    data = _get(url, headers=headers)
    out = []
    for f in (data or {}).get("data") or []:
        out.append({
            "id": str(f.get("id")),
            "name": f.get("displayName") or (f.get("fileName") or ""),
            "version": f.get("displayName", ""),
            "mc": f.get("gameVersions") or [],
            "loaders": [m for m in (f.get("gameVersions") or []) if m.lower() in
                        ("fabric", "forge", "neoforge", "quilt")],
            "url": f.get("downloadUrl") or "",
            "fileName": f.get("fileName"),
            "size": f.get("fileLength", 0),
            "date": (f.get("fileDate") or "")[:10],
            "channel": {1: "release", 2: "beta", 3: "alpha"}.get(f.get("releaseType"), ""),
        })
    return out


def project_versions(source: str, pid: str, mc: str = "", loader: str = "") -> List[Dict[str, Any]]:
    cfg = core.load()
    if source in ("mczwlt", "auto"):
        data = versions_modrinth(pid, mc, loader, cfg)
        if data:
            return data
    if source in ("modrinth", "mczwlt", "auto"):
        try:
            data = versions_modrinth(pid, mc, loader, cfg)
            if data:
                return data
        except Exception:
            pass
    try:
        return versions_curseforge(pid, mc, loader, cfg)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"获取版本列表失败：{e}")


# ---------------- 目标版本解析与兼容性检查 ----------------

import re as _re

# 已知的模组加载器（neoforge 要先于 forge 匹配）
_LOADER_HINTS = ("neoforge", "forge", "fabric", "quilt", "liteloader")
# 光影运行所需的加载器
_SHADER_RUNTIMES = ("iris", "optifine", "oculus")

_MC_RE = _re.compile(r"(\d+\.\d+(?:\.\d+)?(?:-(?:pre|rc|snapshot)[-\w]*)?)$")


def target_info(vid: str, cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    推导目标版本的信息：MC 版本号、模组加载器、是否隔离、实际目录。
    版本 ID 形如：1.21.1 / fabric-loader-0.16.9-1.21.1 / 1.21.1-forge-52.0.0
    """
    cfg = cfg or core.load()
    info: Dict[str, Any] = {"id": vid, "mc": "", "loader": "", "isolate": False, "dir": ""}

    low = vid.lower()
    for name in _LOADER_HINTS:
        if name in low:
            info["loader"] = name
            break

    try:
        cur, seen = vid, set()
        while cur and cur not in seen:
            seen.add(cur)
            d = vmod.read_version_json(cur)
            parent = d.get("inheritsFrom")
            if parent:
                cur = parent
            else:
                info["mc"] = cur
                break
    except Exception:  # noqa: BLE001
        info["mc"] = ""
    if not info["mc"]:
        m = _MC_RE.search(vid)
        info["mc"] = m.group(1) if m else vid

    try:
        info["isolate"] = core.is_isolated(vid, cfg)
        info["dir"] = core.effective_game_dir(vid, cfg)
    except Exception:  # noqa: BLE001
        info["dir"] = ""
    return info


def _ver_key(v: str) -> Tuple[Any, ...]:
    m = _re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", v or "")
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def check_compat(kind: str, vid: str,
                 file_meta: Optional[Dict[str, Any]] = None,
                 cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    检查一个资源与目标版本是否兼容。
    file_meta 形如 {"mc": ["1.21.1"], "loaders": ["fabric"], "name": ...}
    返回 level: ok / warn / bad，reasons 与 reasonsEn 一一对应。
    """
    cfg = cfg or core.load()
    meta = file_meta or {}
    tgt = target_info(vid, cfg)
    mc = tgt.get("mc") or ""
    loader = tgt.get("loader") or ""

    sup_mc = [str(x) for x in (meta.get("mc") or []) if x]
    sup_ld = [str(x).lower() for x in (meta.get("loaders") or []) if x]

    rr: List[Tuple[str, str, str]] = []   # (level, zh, en)

    def add(lv: str, zh: str, en: str) -> None:
        rr.append((lv, zh, en))

    mc_hit = True
    if sup_mc:
        mc_hit = mc in sup_mc
        if not mc_hit:
            base = _re.match(r"(\d+\.\d+)", mc)
            if base:
                mc_hit = any(str(x).startswith(base.group(1)) for x in sup_mc)
    else:
        add("warn", "该资源未声明支持的游戏版本，无法确认兼容性",
            "This content does not declare supported game versions, so compatibility is unknown")

    if not mc_hit and sup_mc:
        nearest = sorted(sup_mc, key=_ver_key)
        near = ", ".join(nearest[-3:]) if len(nearest) > 3 else ", ".join(nearest)
        if kind == "mod":
            add("bad", f"该模组不支持 {mc}，它支持：{near}",
                f"This mod does not support {mc}. Supported: {near}")
        elif kind == "modpack":
            add("bad", f"该整合包基于 {near}，与当前目标 {mc} 不一致",
                f"This modpack targets {near}, which differs from {mc}")
        elif kind == "shader":
            add("warn", f"该光影未标注支持 {mc}（标注支持：{near}），可能显示异常",
                f"This shader is not marked as supporting {mc} (marked: {near}); it may render incorrectly")
        elif kind == "resourcepack":
            lo, hi = _ver_key(nearest[0]), _ver_key(nearest[-1])
            cur = _ver_key(mc)
            if hi and cur > hi:
                add("warn", f"该资源包最高支持 {nearest[-1]}，当前 {mc} 更高，游戏内可能提示版本不符",
                    f"This resource pack supports up to {nearest[-1]}; {mc} is newer and the game may warn about it")
            elif lo and cur < lo:
                add("warn", f"该资源包最低需要 {nearest[0]}，当前 {mc} 更低，可能显示异常",
                    f"This resource pack requires at least {nearest[0]}; {mc} is older and it may render incorrectly")
            else:
                add("warn", f"该资源包未标注支持 {mc}，一般仍可使用但可能提示版本不符",
                    f"This resource pack is not marked for {mc}; it usually still works but the game may warn")
        else:
            add("bad", f"不支持 {mc}，支持：{near}", f"Does not support {mc}. Supported: {near}")

    if kind == "mod":
        if not loader:
            add("bad", "目标版本是原版，没有安装模组加载器，模组不会生效",
                "The target is a vanilla version with no mod loader, so this mod will not work")
        elif sup_ld and loader not in sup_ld:
            known = [x for x in sup_ld if x in _LOADER_HINTS]
            if known:
                add("bad", f"该模组需要 {known[0]}，但目标版本用的是 {loader}",
                    f"This mod requires {known[0]}, but the target version uses {loader}")
        elif not sup_ld:
            add("warn", "该模组未声明所需加载器，加载器方面无法确认",
                "This mod does not declare a required loader, so loader compatibility is unknown")
        elif loader == "quilt" and "fabric" in sup_ld and "quilt" not in sup_ld:
            add("warn", "Quilt 通常能直接运行 Fabric 模组，但并非全部兼容",
                "Quilt can usually run Fabric mods, but not all of them")

    elif kind == "shader":
        runtimes = [x for x in sup_ld if x in _SHADER_RUNTIMES]
        if sup_ld and runtimes and not loader:
            add("warn", f"这个光影需要 {runtimes[0]} 才能生效，当前目标「{vid}」是原版，建议先装 Iris",
                f"This shader requires {runtimes[0]}; target \"{vid}\" is vanilla — install Iris first")
        elif sup_ld and runtimes and loader in ("forge", "neoforge"):
            add("warn", f"这个光影为 {runtimes[0]} 设计，{loader} 环境需搭配 Oculus 等兼容层",
                f"This shader is built for {runtimes[0]}; on {loader} you need a compatibility layer such as Oculus")
        elif not sup_ld and not loader:
            add("warn", "光影需要 Iris / OptiFine 才能生效，当前目标看起来是原版",
                "Shaders need Iris or OptiFine; the target appears to be vanilla")

    elif kind == "modpack":
        if sup_ld and loader and loader not in sup_ld:
            add("bad", f"整合包使用的加载器是 {sup_ld[0]}，与目标版本的 {loader} 不一致",
                f"This modpack uses {sup_ld[0]}, but the target version uses {loader}")

    level = "ok"
    for lv, _zh, _en in rr:
        if lv == "bad":
            level = "bad"
        elif lv == "warn" and level == "ok":
            level = "warn"

    return {
        "level": level,
        "compatible": level == "ok",
        "reasons": [z for _lv, z, _e in rr],
        "reasonsEn": [e for _lv, _z, e in rr],
        "target": tgt,
        "file": {
            "name": meta.get("name") or meta.get("fileName") or "",
            "mc": sup_mc,
            "loaders": sup_ld,
        },
        "kind": kind,
        "kindName": KINDS.get(kind, {}).get("name", kind),
    }


# ---------------- 安装 ----------------

def install_dir(kind: str, vid: str, cfg: Dict[str, Any] | None = None) -> str:
    """按资源类型和版本隔离规则算出落地目录。"""
    cfg = cfg or core.load()
    base = core.effective_game_dir(vid, cfg)
    sub = KINDS.get(kind, {}).get("dir", "mods")
    d = os.path.join(base, sub) if sub else base
    os.makedirs(d, exist_ok=True)
    return d


def download_resource(url: str, dest: str) -> str:
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".cbtmp"
    last: Optional[Exception] = None
    for _ in range(3):
        try:
            with requests.get(url, stream=True, timeout=(8, 60),
                              headers={"User-Agent": UA, "Connection": "close"}) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        if chunk:
                            f.write(chunk)
            if os.path.getsize(tmp) < 64:
                raise ValueError("文件过小，可能不是有效资源")
            if os.path.exists(dest):
                os.remove(dest)
            os.replace(tmp, dest)
            return dest
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5)
    raise last or RuntimeError("下载失败")


def install_file(kind: str, vid: str, url: str, file_name: str) -> Dict[str, Any]:
    """下载单个资源文件到目标版本的目录。"""
    cfg = core.load()
    d = install_dir(kind, vid, cfg)
    name = file_name or os.path.basename(url.split("?")[0]) or "download.jar"
    dest = os.path.join(d, name)
    download_resource(url, dest)
    try:
        tgt = target_info(vid, cfg)
    except Exception:  # noqa: BLE001
        tgt = {"id": vid, "mc": "", "loader": "", "dir": d}
    return {"ok": True, "path": dest, "dir": d, "file": name, "kind": kind, "target": tgt}


def install_modpack(url: str, vid: str, task=None) -> Dict[str, Any]:
    """
    安装整合包。支持 Modrinth .mrpack 与 CurseForge 导出包（manifest.json）。
    会把 overrides 解压到版本目录、把 mod 下载到 mods 目录。
    """
    cfg = core.load()
    base = core.effective_game_dir(vid, cfg)
    tmp = os.path.join(core.ROOT_DIR, "tmp", "modpack-" + str(int(time.time())) + ".zip")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    download_resource(url, tmp)

    mods_dir = os.path.join(base, "mods")
    os.makedirs(mods_dir, exist_ok=True)
    count = 0
    need_loader: Dict[str, str] = {}

    with zipfile.ZipFile(tmp) as z:
        names = z.namelist()
        if "modrinth.index.json" in names:
            idx = json.loads(z.read("modrinth.index.json"))
            for k, val in (idx.get("dependencies") or {}).items():
                need_loader[k] = val
            # overrides
            for n in names:
                if n.startswith("overrides/") and not n.endswith("/"):
                    rel = n[len("overrides/"):]
                    tgt = os.path.join(base, rel)
                    os.makedirs(os.path.dirname(tgt), exist_ok=True)
                    with z.open(n) as s, open(tgt, "wb") as o:
                        o.write(s.read())
            files = [f for f in idx.get("files") or []
                     if (f.get("env") or {}).get("client", "required") != "unsupported"]
            total = len(files)
            for i, f in enumerate(files):
                if task:
                    task.message = f"下载模组 {i + 1}/{total}"
                    task.total = total
                    task.done = i
                hashes = f.get("hashes") or {}
                dl = (f.get("downloads") or [])
                u = dl[0] if dl else ""
                if not u:
                    sha = hashes.get("sha512") or hashes.get("sha1")
                    if sha:
                        u = f"https://api.modrinth.com/v2/version_file/{sha}"
                if not u:
                    continue
                p = f.get("path") or ""
                dest = os.path.join(base, p) if p else os.path.join(mods_dir, os.path.basename(u))
                if p.startswith("mods/"):
                    dest = os.path.join(base, p)
                try:
                    download_resource(u, dest)
                    count += 1
                except Exception:
                    continue
        elif "manifest.json" in names:
            man = json.loads(z.read("manifest.json"))
            mc = (man.get("minecraft") or {}).get("version", "")
            modloader = (man.get("minecraft") or {}).get("modLoaders") or []
            if modloader:
                lid = (modloader[0] or {}).get("id", "")
                if "-" in lid:
                    need_loader[lid.split("-")[0]] = lid.split("-", 1)[1]
            over = (man.get("overrides") or "overrides")
            prefix = over + "/"
            for n in names:
                if n.startswith(prefix) and not n.endswith("/"):
                    rel = n[len(prefix):]
                    tgt = os.path.join(base, rel)
                    os.makedirs(os.path.dirname(tgt), exist_ok=True)
                    with z.open(n) as s, open(tgt, "wb") as o:
                        o.write(s.read())
            files = man.get("files") or []
            total = len(files)
            for i, f in enumerate(files):
                if task:
                    task.message = f"下载模组 {i + 1}/{total}"
                    task.total = total
                    task.done = i
                pid, fid = f.get("projectID"), f.get("fileID")
                u = ""
                try:
                    d = _get(cf_base(cfg) + f"/mods/{pid}/files/{fid}")
                    if d and d.get("data"):
                        u = (d["data"] or {}).get("downloadUrl") or ""
                        fn = (d["data"] or {}).get("fileName") or f"{pid}-{fid}.jar"
                    else:
                        fn = f"{pid}-{fid}.jar"
                except Exception:
                    fn = f"{pid}-{fid}.jar"
                if not u:
                    continue
                try:
                    download_resource(u, os.path.join(mods_dir, fn))
                    count += 1
                except Exception:
                    continue
            if mc:
                need_loader.setdefault("_mc", mc)
        else:
            raise RuntimeError("无法识别的整合包格式（缺少 modrinth.index.json / manifest.json）")

    try:
        os.remove(tmp)
    except Exception:
        pass
    return {"ok": True, "count": count, "dir": base, "needLoader": need_loader}


# ==================================================================
# 外挂 / 作弊内容拦截（拒绝下载）
# ==================================================================

# 强命中：明确的作弊客户端或作弊功能
_CHEAT_HARD = (
    "wurst", "impact client", "meteor client", "liquidbounce", "sigma client",
    "aristois", "future client", "rusherhack", "bleachhack",
    "xray", "x-ray", "x_ray", "orexray", "xraymod",
    "killaura", "kill aura", "aimbot", "aim bot", "triggerbot",
    "speedhack", "speed hack", "flyhack", "fly hack",
    "anticheat bypass", "bypass anticheat", "nofall", "no-fall", "jesus hack",
    "seedcracker", "seed cracker", "dupemod", "dupe mod",
    "hacked client", "hack client", "hacking client",
)

# 弱命中：需要结合上下文，出现在名称中才判定
_CHEAT_SOFT = (
    "flight", "creative fly", "flymod", "flying mod", "elytra cheat",
    "reach hack", "reach distance", "hitbox expand", "attackreach",
    "autoclicker", "auto clicker", "click cheat",
    "wallhack", "wall hack", "esp hack", "player esp", "tracer hack",
    "aim assist", "autoaim", "auto aim",
)

# 良性例外：含这些词不算作弊（避免误伤）
_CHEAT_WHITE = (
    "butterfly", "firefly", "dragonfly", "flywheel", "fireflies", "moth",
    "reacharound", "reaching", "reach entity", "outline", "flytrap",
    "starfly", "fruit fly", "fly fishing", "soaring", "glider",
    "flightpath", "preflight",
)

_CHEAT_SOFT_DESC = {
    "flight": "客户端飞行属于外挂功能，多人服务器会判定为作弊",
    "fly hack": "客户端飞行属于外挂功能，多人服务器会判定为作弊",
    "xray": "透视类功能属于外挂，会破坏多人公平性",
    "killaura": "自动攻击属于外挂，会破坏多人公平性",
    "reach": "攻击距离修改属于外挂，会破坏多人公平性",
}


def _norm_text(s: str) -> str:
    return (s or "").lower().replace("_", " ").replace("-", " ")


def check_cheat(name: str = "", summary: str = "", file_name: str = "") -> Dict[str, Any]:
    """
    判断资源是否属于外挂/作弊内容（客户端飞行、透视、杀戮光环等）。
    返回 {"blocked": bool, "level": "ok|soft|hard", "reason": str}
    """
    nm = _norm_text(name)
    fn = _norm_text(file_name)
    sm = _norm_text(summary)
    blob = " ".join(x for x in (nm, fn) if x)
    show = name or file_name or ""

    for w in _CHEAT_WHITE:
        if w in blob:
            return {"blocked": False, "level": "ok", "reason": ""}

    for w in _CHEAT_HARD:
        nw = _norm_text(w)
        if nw in blob or (sm and nw in sm):
            return {"blocked": True, "level": "hard",
                    "reason": f"「{show}」属于外挂类内容（命中：{w}），"
                              f"脆冰启动器不提供外挂与作弊功能的下载"}

    for w in _CHEAT_SOFT:
        if _norm_text(w) in blob:
            return {"blocked": True, "level": "hard",
                    "reason": f"「{show}」疑似外挂类内容（命中：{w}），"
                              f"会影响多人游戏公平性，脆冰启动器不提供此类下载"}

    # 描述里的模糊词只提醒，不拦截
    for w, why in _CHEAT_SOFT_DESC.items():
        if w in sm:
            return {"blocked": False, "level": "soft", "reason": why}

    return {"blocked": False, "level": "ok", "reason": ""}


def filter_cheats(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """过滤搜索结果，移除被判定为外挂的条目。"""
    keep: List[Dict[str, Any]] = []
    for p in results or []:
        c = check_cheat(p.get("name", ""), p.get("summary", ""), "")
        if c["blocked"]:
            continue
        if c["level"] == "soft":
            p["warn"] = c["reason"]
        keep.append(p)
    return keep
