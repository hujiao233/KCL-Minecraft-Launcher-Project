# -*- coding: utf-8 -*-
"""
脆冰启动器 - 版本管理
负责：版本清单拉取、版本 JSON 解析（含继承合并）、原版完整下载安装。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

import core
from core import DownloadItem, download_all


REQUEST_TIMEOUT = 20

SOURCES = {
    "bmclapi": {
        "name": "BMCLAPI 镜像（国内推荐）",
        "manifest": "https://bmclapi2.bangbang93.com/mc/game/version_manifest.json",
        "version_json": "https://bmclapi2.bangbang93.com/version/{vid}/json",
        "client_jar": "https://bmclapi2.bangbang93.com/version/{vid}/client",
        "assets_index": "https://bmclapi2.bangbang93.com/assets/indexes/{idx}.json",
        "asset": "https://bmclapi2.bangbang93.com/assets/{h0}/{hash}",
        "library": "https://bmclapi2.bangbang93.com/libraries/{path}",
    },
    "official": {
        "name": "Mojang 官方源",
        "manifest": "https://piston-meta.mojang.com/mc/game/version_manifest.json",
        "version_json": "",           # 直接用 manifest 里的 url
        "client_jar": "",             # 直接用 version json 里的 url
        "assets_index": "",           # 直接用 version json 里的 url
        "asset": "https://resources.download.minecraft.net/{h0}/{hash}",
        "library": "https://libraries.minecraft.net/{path}",
    },
}

# 每个文件都会按「当前源 → 备用源」生成候选地址，节点失效时自动切换
FALLBACK_ORDER = {"bmclapi": "official", "official": "bmclapi"}


def _candidates(cfg: Dict[str, Any], key: str, **fmt) -> List[str]:
    """按当前下载源优先的顺序生成候选 URL 列表。"""
    cur = cfg.get("downloadSource", "bmclapi")
    out: List[str] = []
    for sid in (cur, FALLBACK_ORDER.get(cur, "")):
        src = SOURCES.get(sid)
        if not src:
            continue
        tpl = src.get(key) or ""
        if not tpl:
            continue
        try:
            u = tpl.format(**fmt)
        except Exception:
            continue
        if u and u not in out:
            out.append(u)
    return out

_manifest_cache: Optional[Dict[str, Any]] = None
_manifest_time: float = 0.0
MANIFEST_TTL = 300  # 5 分钟


def get_source(cfg: Dict[str, Any] | None = None) -> Dict[str, str]:
    cfg = cfg or core.load()
    return SOURCES.get(cfg.get("downloadSource", "bmclapi"), SOURCES["bmclapi"])


# ---------------- 版本清单 ----------------

def fetch_manifest(force: bool = False) -> Dict[str, Any]:
    """拉取版本清单，带内存缓存与磁盘缓存。"""
    global _manifest_cache, _manifest_time
    if not force and _manifest_cache and time.time() - _manifest_time < MANIFEST_TTL:
        return _manifest_cache

    cfg = core.load()
    src = get_source(cfg)
    cache_file = os.path.join(core.ROOT_DIR, "manifest.json")

    # 当前源 → 备用源 → 磁盘/内存缓存：任一镜像抽风都不至于整个列表拿不到
    cur = cfg.get("downloadSource", "bmclapi")
    data, last_err, seen = None, None, set()
    for sid in (cur, FALLBACK_ORDER.get(cur, "")):
        s = SOURCES.get(sid)
        url = (s or {}).get("manifest") or ""
        if not url or url in seen:
            continue
        seen.add(url)
        try:
            r = requests.get(url, timeout=REQUEST_TIMEOUT,
                             headers={"User-Agent": "CuibingLauncher/0.1"})
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:  # noqa: BLE001
            last_err = e

    if data is None:
        # 网络不通时退回磁盘 / 内存缓存
        if _manifest_cache:
            return _manifest_cache
        if os.path.isfile(cache_file):
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            raise last_err
    else:
        try:
            core.ensure_dirs()
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception:  # noqa: BLE001
            pass

    _manifest_cache = data
    _manifest_time = time.time()
    return data


# ---------------- 版本分类 ----------------
#
# Minecraft 的两套命名规则：
#   老式正式版：1.13 / 1.14.4 / 1.21.11
#   新式正式版：26.1 / 26.1.2 / 26.3          （年号 . 序号）
#   新式快照  ：26.3-snapshot-5
#   新式预发布：26.3-pre-1 / 26.3-rc-1
#   周格式    ：18w43a（老式快照）
#              26w14a（2026 年起周格式只用于愚人节）
#
# 脆冰启动器的提供策略：
#   正式版   ：1.13 扁平化之后的所有正式版
#   快照     ：26.1 及之后
#   预发布   ：27.1 及之后（未来版本）
#   愚人节   ：27 年起
#   1.13 之前：不提供

RELEASE_MIN = (1, 13, 0)
SNAPSHOT_MIN = (26, 1, 0)
PRERELEASE_MIN = (27, 1, 0)
APRILFOOLS_MIN_YEAR = 27

CATS = {
    "release": "正式版",
    "snapshot": "快照",
    "prerelease": "预发布",
    "aprilfools": "愚人节",
}

_NEW_RE = re.compile(r"^(\d{2})\.(\d+)(?:\.(\d+))?(?:-(snapshot|pre|rc)-(\d+))?$")
_OLD_RE = re.compile(r"^1\.(\d+)(?:\.(\d+))?(?:\.(\d+))?")
_WEEK_RE = re.compile(r"^(\d{2})w(\d{2})(.*)$", re.I)
_OLD_PRE_RE = re.compile(r"(?:-pre\d*|-rc\d*|\s+pre-release|\s+release\s+candidate)", re.I)


def classify(vid: str, vtype: str = "release") -> Dict[str, Any]:
    """
    判断一个版本属于哪一类，并返回可比较的排序键。
    返回 {cat, key, seq, year}
    """
    vid = vid or ""
    m = _NEW_RE.match(vid)
    if m:
        year = int(m.group(1))
        minor = int(m.group(2))
        patch = int(m.group(3) or 0)
        tag = m.group(4)
        seq = int(m.group(5) or 0)
        key = (year, minor, patch)
        if tag == "snapshot":
            return {"cat": "snapshot", "key": key, "seq": seq, "year": year}
        if tag in ("pre", "rc"):
            return {"cat": "prerelease", "key": key, "seq": seq, "year": year}
        return {"cat": "release", "key": key, "seq": 0, "year": year}

    w = _WEEK_RE.match(vid)
    if w:
        year = int(w.group(1))
        week = int(w.group(2))
        suffix = w.group(3) or ""
        # 2026 年起周格式不再用于常规快照；另外历史上愚人节版本都带长后缀
        if year >= 26 or len(suffix) > 1:
            return {"cat": "aprilfools", "key": (year, week, 0), "seq": 0, "year": year}
        # 老式周快照（18w43a 之类）不提供，排序键压到最低即可
        return {"cat": "snapshot", "key": (0, 0, week), "seq": 0, "year": year}

    o = _OLD_RE.match(vid)
    if o:
        if vtype == "release" and not _OLD_PRE_RE.search(vid):
            key = (1, int(o.group(1)), int(o.group(2) or 0))
            return {"cat": "release", "key": key, "seq": 0, "year": 0}
        if _OLD_PRE_RE.search(vid):
            key = (1, int(o.group(1)), int(o.group(2) or 0))
            return {"cat": "prerelease", "key": key, "seq": 0, "year": 0}
        return {"cat": "snapshot", "key": (1, int(o.group(1)), int(o.group(2) or 0)),
                "seq": 0, "year": 0}

    return {"cat": "other", "key": (0, 0, 0), "seq": 0, "year": 0}


def version_allowed(vid: str, vtype: str = "release") -> Optional[str]:
    """返回该版本属于哪个可下载分类；不满足提供策略则返回 None。"""
    info = classify(vid, vtype)
    cat, key = info["cat"], info["key"]
    if cat == "release":
        return "release" if key >= RELEASE_MIN else None
    if cat == "snapshot":
        return "snapshot" if key >= SNAPSHOT_MIN else None
    if cat == "prerelease":
        return "prerelease" if key >= PRERELEASE_MIN else None
    if cat == "aprilfools":
        return "aprilfools" if info["year"] >= APRILFOOLS_MIN_YEAR else None
    return None


def list_online_versions(force: bool = False, cat: str = "all",
                         query: str = "") -> List[Dict[str, Any]]:
    """
    返回可选下载的版本列表（倒序：最新在前）。
    cat: all | release | snapshot | prerelease | aprilfools
    """
    m = fetch_manifest(force)
    out: List[Dict[str, Any]] = []
    for v in m.get("versions", []):
        vid = v.get("id", "")
        vtype = v.get("type", "release")
        if vtype in ("old_alpha", "old_beta"):
            continue                      # 1.13 之前的远古版本一律不提供
        c = version_allowed(vid, vtype)
        if not c:
            continue
        if cat != "all" and c != cat:
            continue
        if query and query.lower() not in vid.lower():
            continue
        out.append({
            "id": vid,
            "type": vtype,
            "cat": c,
            "catName": CATS.get(c, c),
            "releaseTime": v.get("releaseTime", ""),
            "url": v.get("url", ""),
            "sha1": v.get("sha1", ""),
        })
    # 官方清单是"新→旧"，但各镜像顺序不保证；按发布时间倒序排一次，
    # 保证最新的版本永远在最前面。
    out.sort(key=lambda v: v.get("releaseTime") or "", reverse=True)
    return out


def online_counts(force: bool = False) -> Dict[str, int]:
    """各类别可下载数量（用于界面页签角标）。"""
    m = fetch_manifest(force)
    counts = {"all": 0, "release": 0, "snapshot": 0, "prerelease": 0, "aprilfools": 0}
    for v in m.get("versions", []):
        if v.get("type") in ("old_alpha", "old_beta"):
            continue
        c = version_allowed(v.get("id", ""), v.get("type", "release"))
        if c:
            counts[c] += 1
            counts["all"] += 1
    return counts


# ---------------- 版本 JSON ----------------

def download_version_json(vid: str, version_url: str = "") -> str:
    """下载版本 JSON 到本地，返回路径。"""
    cfg = core.load()
    src = get_source(cfg)
    dest = core.version_json_path(vid, cfg)
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    # 候选：当前源的镜像接口 → 清单里的官方地址
    urls = _candidates(cfg, "version_json", vid=vid)
    if not version_url:
        for v in fetch_manifest().get("versions", []):
            if v.get("id") == vid:
                version_url = v.get("url", "")
                break
    if version_url and version_url not in urls:
        urls.append(version_url)
    if not urls:
        raise ValueError(f"找不到版本 {vid} 的下载地址")

    last_err: Optional[Exception] = None
    data = None
    for u in urls:
        try:
            r = requests.get(u, timeout=REQUEST_TIMEOUT,
                             headers={"User-Agent": "CuibingLauncher/0.1"})
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
    if data is None:
        raise last_err or RuntimeError(f"版本 {vid} 信息下载失败")

    with open(dest, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return dest


def read_version_json(vid: str) -> Dict[str, Any]:
    """读取本地版本 JSON，不存在则尝试联网下载。"""
    cfg = core.load()
    p = core.version_json_path(vid, cfg)
    if not os.path.isfile(p):
        download_version_json(vid)
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _merge_args(base: List[Any], extra: List[Any]) -> List[Any]:
    """合并 arguments.jvm/game 列表，去重并保持顺序。"""
    out = list(base)
    seen = {json.dumps(x, sort_keys=True) if isinstance(x, dict) else str(x) for x in base}
    for x in extra:
        key = json.dumps(x, sort_keys=True) if isinstance(x, dict) else str(x)
        if key not in seen:
            out.append(x)
            seen.add(key)
    return out


def resolve_version(vid: str) -> Dict[str, Any]:
    """
    解析版本信息，处理 inheritsFrom 继承链。
    返回合并后的版本 dict，并附带 _id 字段。
    """
    def _load(cur: str) -> Dict[str, Any]:
        return read_version_json(cur)

    def _walk(cur: str) -> List[Dict[str, Any]]:
        chain = []
        while True:
            vj = _load(cur)
            chain.append(vj)
            parent = vj.get("inheritsFrom")
            if not parent:
                break
            cur = parent
        return chain

    chain = _walk(vid)
    # chain[0] 是最终版本，最后是根版本（通常是某个原版）
    merged: Dict[str, Any] = {}
    for vj in reversed(chain):
        for k, v in vj.items():
            if k in ("inheritsFrom",):
                continue
            if k == "arguments":
                old_args = merged.get("arguments", {})
                merged["arguments"] = {
                    "game": _merge_args(old_args.get("game", []), v.get("game", [])),
                    "jvm": _merge_args(old_args.get("jvm", []), v.get("jvm", [])),
                }
            elif k == "libraries":
                merged["libraries"] = list(v) + merged.get("libraries", [])
            else:
                merged[k] = v

    # 老版本字段兜底
    merged.setdefault("id", vid)
    merged["_id"] = vid
    merged["_jarOf"] = get_jar_version(merged, vid)
    return merged


def get_jar_version(vj: Dict[str, Any], vid: str) -> str:
    """确定主 jar 使用哪个版本（有 jar 字段则用它，否则往上找不带 jar 的原版）。"""
    cur = vid
    seen = set()
    while cur and cur not in seen:
        seen.add(cur)
        try:
            d = read_version_json(cur)
        except Exception:
            return vid
        if d.get("jar"):
            return d["jar"]
        parent = d.get("inheritsFrom")
        if not parent:
            return cur
        cur = parent
    return vid


# ---------------- 规则判定 ----------------

def _rule_match(rule: Dict[str, Any], features: Dict[str, bool]) -> bool:
    """判断单条 rule 是否命中当前环境（Windows 64 位）。"""
    if "os" in rule:
        osr = rule["os"] or {}
        if osr.get("name") and osr["name"] != "windows":
            return False
        ver = osr.get("version")
        if ver and not _version_match_windows(ver):
            return False
        arch = osr.get("arch")
        if arch and arch != "x86" and arch != "x64":
            return False
    if "features" in rule:
        for k, v in (rule["features"] or {}).items():
            if bool(features.get(k, False)) != bool(v):
                return False
    return True


def _version_match_windows(pattern: str) -> bool:
    """极简正则匹配 Windows 版本号，够用即可。"""
    import re
    try:
        return re.search(pattern, "10.0") is not None
    except re.error:
        return True


def library_allowed(lib: Dict[str, Any], features: Dict[str, bool] | None = None) -> bool:
    features = features or {}
    rules = lib.get("rules")
    if not rules:
        return True
    allowed = False
    for rule in rules:
        if _rule_match(rule, features):
            allowed = (rule.get("action") == "allow")
    return allowed


def argument_allowed(arg: Any, features: Dict[str, bool]) -> bool:
    if not isinstance(arg, dict):
        return True
    rules = arg.get("rules")
    if not rules:
        return True
    allowed = False
    for rule in rules:
        if _rule_match(rule, features):
            allowed = (rule.get("action") == "allow")
    return allowed


# ---------------- 库文件 / 资源 ----------------

def library_path(lib: Dict[str, Any]) -> str:
    """把 maven name 转成相对路径 com/mojang/.../x-y.jar"""
    name: str = lib["name"]
    ext = lib.get("downloads", {}).get("artifact", {}).get("extension", "jar")
    parts = name.split(":")
    group, artifact, version = parts[0], parts[1], parts[2] if len(parts) > 2 else ""
    if len(parts) > 3:
        version = f"{parts[2]}-{parts[3]}"      # classifier
    return "/".join([group.replace(".", "/"), artifact, version,
                     f"{artifact}-{version}.{ext}"])


def library_local_path(lib: Dict[str, Any], cfg: Dict[str, Any] | None = None) -> str:
    return os.path.join(core.libraries_dir(cfg), library_path(lib).replace("/", os.sep))


def collect_libraries(vj: Dict[str, Any], cfg: Dict[str, Any] | None = None) -> List[DownloadItem]:
    cfg = cfg or core.load()
    items: List[DownloadItem] = []
    seen = set()
    for lib in vj.get("libraries", []):
        if not library_allowed(lib):
            continue
        artifacts = lib.get("downloads", {}).get("artifact")
        if artifacts and artifacts.get("path"):
            rel = artifacts["path"]
            urls = _candidates(cfg, "library", path=rel)
            if artifacts.get("url") and artifacts["url"] not in urls:
                urls.append(artifacts["url"])
            dest = os.path.join(core.libraries_dir(cfg), rel.replace("/", os.sep))
            if dest in seen:
                continue
            seen.add(dest)
            items.append(DownloadItem(urls[0] if urls else "", dest,
                                      size=artifacts.get("size", 0),
                                      sha1=artifacts.get("sha1", ""),
                                      name=os.path.basename(rel),
                                      urls=urls))
        else:
            # 版本 JSON 未提供 artifact，说明该库只有 natives 包（如 lwjgl-platform），
            # 主 jar 并不存在，硬拼 URL 只会得到 404，因此直接跳过。
            continue

        # natives jar
        natives = lib.get("natives") or {}
        if "windows" in natives:
            cls = natives["windows"].replace("${arch}", "64")
            cdl = (lib.get("downloads") or {}).get("classifiers") or {}
            info = cdl.get(cls)
            if info and info.get("path"):
                rel = info["path"]
                urls = _candidates(cfg, "library", path=rel)
                if info.get("url") and info["url"] not in urls:
                    urls.append(info["url"])
                dest = os.path.join(core.libraries_dir(cfg), rel.replace("/", os.sep))
                if dest not in seen and urls:
                    seen.add(dest)
                    items.append(DownloadItem(urls[0], dest,
                                              size=info.get("size", 0),
                                              sha1=info.get("sha1", ""),
                                              name=os.path.basename(rel),
                                              urls=urls))
    return items


def native_jars(vj: Dict[str, Any], cfg: Dict[str, Any] | None = None) -> List[str]:
    """返回需要解压的 natives jar 本地路径。"""
    cfg = cfg or core.load()
    out = []
    for lib in vj.get("libraries", []):
        if not library_allowed(lib):
            continue
        natives = lib.get("natives") or {}
        if "windows" not in natives:
            continue
        cls = natives["windows"].replace("${arch}", "64")
        cdl = (lib.get("downloads") or {}).get("classifiers") or {}
        info = cdl.get(cls)
        if info and info.get("path"):
            p = os.path.join(core.libraries_dir(cfg), info["path"].replace("/", os.sep))
            if os.path.isfile(p):
                out.append(p)
    return out


def collect_client_jar(vj: Dict[str, Any], cfg: Dict[str, Any] | None = None) -> Optional[DownloadItem]:
    cfg = cfg or core.load()
    # 主 jar 存到其所属原版版本目录下（Forge 等继承版本会复用同一个 jar）
    vid = vj.get("_jarOf") or vj.get("id")
    dest = core.version_jar_path(vid, cfg)

    info = (vj.get("downloads") or {}).get("client") or {}
    urls = _candidates(cfg, "client_jar", vid=vid)
    if info.get("url") and info["url"] not in urls:
        urls.append(info["url"])
    if not urls:
        return None
    return DownloadItem(urls[0], dest, size=info.get("size", 0),
                        sha1=info.get("sha1", ""), name=f"{vid}.jar", urls=urls)


def collect_assets(vj: Dict[str, Any], cfg: Dict[str, Any] | None = None,
                   include_virtual: bool = False) -> List[DownloadItem]:
    """下载资源索引里的全部 object。"""
    cfg = cfg or core.load()
    src = get_source(cfg)
    idx_id = (vj.get("assetIndex") or {}).get("id") or vj.get("assets")
    if not idx_id:
        return []

    idx_dir = os.path.join(core.assets_dir(cfg), "indexes")
    os.makedirs(idx_dir, exist_ok=True)
    idx_path = os.path.join(idx_dir, f"{idx_id}.json")

    idx_info = vj.get("assetIndex") or {}
    urls = _candidates(cfg, "assets_index", idx=idx_id)
    if idx_info.get("url") and idx_info["url"] not in urls:
        urls.append(idx_info["url"])

    if not os.path.isfile(idx_path):
        data = None
        last_err: Optional[Exception] = None
        for u in urls:
            try:
                r = requests.get(u, timeout=REQUEST_TIMEOUT,
                                 headers={"User-Agent": "CuibingLauncher/0.1"})
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
        if data is None:
            raise last_err or RuntimeError(f"资源索引 {idx_id} 下载失败")
        with open(idx_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    if not os.path.isfile(idx_path):
        return []
    with open(idx_path, "r", encoding="utf-8") as f:
        index = json.load(f)

    obj_root = os.path.join(core.assets_dir(cfg), "objects")
    items: List[DownloadItem] = []
    seen = set()
    for _name, obj in (index.get("objects") or {}).items():
        h: str = obj.get("hash", "")
        if not h or h in seen:
            continue
        seen.add(h)
        dest = os.path.join(obj_root, h[:2], h)
        if os.path.isfile(dest):
            continue
        urls = _candidates(cfg, "asset", h0=h[:2], hash=h)
        if not urls:
            continue
        items.append(DownloadItem(urls[0], dest, urls=urls,
                                  size=obj.get("size", 0), sha1=h, name=h[:8]))
    return items


def collect_logging(vj: Dict[str, Any],
                    cfg: Dict[str, Any] | None = None) -> Tuple[Optional[DownloadItem], str]:
    """返回 (日志配置文件下载项, JVM 参数)。"""
    cfg = cfg or core.load()
    info = (vj.get("logging") or {}).get("client") or {}
    arg = info.get("argument", "")
    f = info.get("file") or {}
    url = f.get("url") or ""
    fid = f.get("id") or ""
    if not url or not fid:
        return None, arg
    dest = os.path.join(core.assets_dir(cfg), "log_configs", os.path.basename(fid))
    urls = [url]
    # 日志配置镜像地址（BMCLAPI 走同一套 object 存储）
    alt = url.replace("https://game.mojang.com", "https://bmclapi2.bangbang93.com")
    alt = alt.replace("https://piston-data.mojang.com", "https://bmclapi2.bangbang93.com")
    if alt and alt != url and alt not in urls:
        urls.append(alt)
    return DownloadItem(urls[0], dest, urls=urls,
                        size=f.get("size", 0), sha1=f.get("sha1", ""),
                        name=os.path.basename(fid)), arg


# ---------------- 安装 ----------------

def install_version(vid: str, task, with_assets: bool = True) -> None:
    """完整安装一个版本：json -> jar -> libraries -> assets。"""
    cfg = core.load()

    def _check_cancel():
        if task.cancelled:
            task.state = core.TaskState.CANCELLED
            raise RuntimeError("已取消")
        return False

    task.total = 0
    task.done = 0

    # 1. 版本 JSON
    task.message = "获取版本信息…"
    _check_cancel()
    try:
        read_version_json(vid)
    except Exception as e:  # noqa: BLE001
        task.error = f"版本信息获取失败：{e}"
        task.state = core.TaskState.FAILED
        return

    vj = resolve_version(vid)

    # 2. 汇总待下载文件
    task.message = "解析依赖清单…"
    _check_cancel()
    items: List[DownloadItem] = []

    jar_item = collect_client_jar(vj, cfg)
    if jar_item and not core.file_ok(jar_item):
        items.append(jar_item)

    task.message = "检查运行库…"
    libs = collect_libraries(vj, cfg)
    items.extend([i for i in libs if not core.file_ok(i)])

    log_item, _log_arg = collect_logging(vj, cfg)
    if log_item and not core.file_ok(log_item):
        items.append(log_item)

    assets: List[DownloadItem] = []
    if with_assets:
        task.message = "检查游戏资源…"
        _check_cancel()
        try:
            assets = collect_assets(vj, cfg)
            items.extend(assets)
        except Exception as e:  # noqa: BLE001
            task.message = f"资源索引获取失败（已跳过资源）：{e}"

    total_bytes = sum(i.size for i in items if i.size)
    if total_bytes:
        task.total = total_bytes
    else:
        task.total = max(1, len(items))

    if not items:
        task.done = task.total
        task.message = "已是最新，无需下载"
        task.state = core.TaskState.SUCCESS
        return

    task.message = f"共 {len(items)} 个文件"

    def _on_bytes(n: int):
        task.done += n
        task.speed = n

    def _on_file(item: DownloadItem, n: int, total: int):
        task.current_file = item.name
        if not total_bytes:
            task.total = total
            task.done = n
        else:
            # 用字节数为准，这里只更新提示
            task.message = f"{n}/{total}"

    ok, failures = download_all(
        items,
        workers=int(cfg.get("maxDownloadThreads", 32)),
        on_file_done=_on_file,
        on_progress=_on_bytes,
        should_stop=lambda: task.cancelled,
    )

    # 3. 解压 natives
    if not task.cancelled:
        task.message = "解压本地库…"
        try:
            extract_natives(vid)
        except Exception as e:  # noqa: BLE001
            failures.append(("natives", str(e)))

    if failures:
        head = failures[0]
        task.error = f"{len(failures)} 个文件失败：{head[0]} - {head[1]}"
        task.state = core.TaskState.FAILED
    else:
        task.message = f"安装完成，共 {ok} 个文件"
        task.done = task.total
        task.state = core.TaskState.SUCCESS


def extract_natives(vid: str, cfg: Dict[str, Any] | None = None) -> None:
    """把 natives jar 解压到版本目录，跳过 META-INF。"""
    import zipfile
    cfg = cfg or core.load()
    vj = resolve_version(vid)
    dest = core.natives_dir(vid, cfg)
    for jar in native_jars(vj, cfg):
        with zipfile.ZipFile(jar) as z:
            for name in z.namelist():
                if name.upper().startswith("META-INF") or name.endswith("/"):
                    continue
                target = os.path.join(dest, name)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                if os.path.isfile(target) and os.path.getsize(target) == z.getinfo(name).file_size:
                    continue
                with z.open(name) as src, open(target, "wb") as out:
                    out.write(src.read())


# ---------------- 已安装版本 ----------------

def list_installed() -> List[Dict[str, Any]]:
    """扫描 versions 目录，返回已安装版本。"""
    cfg = core.load()
    vdir = core.versions_dir(cfg)
    out = []
    if not os.path.isdir(vdir):
        return out
    for name in sorted(os.listdir(vdir)):
        d = os.path.join(vdir, name)
        if not os.path.isdir(d):
            continue
        jf = os.path.join(d, f"{name}.json")
        if not os.path.isfile(jf):
            continue
        try:
            with open(jf, "r", encoding="utf-8") as f:
                vj = json.load(f)
        except Exception:
            continue
        jp = os.path.join(d, f"{name}.jar")
        mtime = os.path.getmtime(jf)
        out.append({
            "id": name,
            "type": vj.get("type", "unknown"),
            "hasJar": os.path.isfile(jp),
            "jarSize": os.path.getsize(jp) if os.path.isfile(jp) else 0,
            "inheritsFrom": vj.get("inheritsFrom", ""),
            "releaseTime": vj.get("releaseTime", ""),
            "assetIndex": (vj.get("assetIndex") or {}).get("id", vj.get("assets", "")),
            "installTime": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)),
        })
    return out


def delete_version(vid: str) -> bool:
    import shutil
    cfg = core.load()
    d = core.version_dir(vid, cfg)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
        return not os.path.isdir(d)
    return False


# ==================================================================
# 模组加载器（Fabric / Quilt / Forge / NeoForge）
# ==================================================================

UA = core.UA

FABRIC_META = ["https://bmclapi2.bangbang93.com/fabric-meta/v2",
               "https://meta.fabricmc.net/v2"]
QUILT_META = ["https://bmclapi2.bangbang93.com/quilt-meta/v3",
              "https://meta.quiltmc.org/v3"]
BMCL = "https://bmclapi2.bangbang93.com"

LOADER_NAMES = {
    "fabric": "Fabric",
    "quilt": "Quilt",
    "forge": "Forge",
    "neoforge": "NeoForge",
}


def _http_json(urls: List[str], timeout=(6, 15)) -> Any:
    """依次尝试多个地址，返回第一个成功的 JSON。"""
    last: Optional[Exception] = None
    for u in urls:
        for _ in range(2):
            try:
                r = requests.get(u, timeout=timeout,
                                 headers={"User-Agent": UA, "Connection": "close"})
                if r.status_code == 200:
                    return r.json()
                last = RuntimeError(f"HTTP {r.status_code}")
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(0.3)
    raise last or RuntimeError("请求失败")


def fabric_loaders(mc: str) -> List[Dict[str, Any]]:
    data = _http_json([b + f"/versions/loader/{mc}" for b in FABRIC_META])
    out = []
    for d in data or []:
        L = d.get("loader") or {}
        if L.get("version"):
            out.append({"version": L["version"], "stable": bool(L.get("stable"))})
    return out


def quilt_loaders(mc: str) -> List[Dict[str, Any]]:
    data = _http_json([b + f"/versions/loader/{mc}" for b in QUILT_META])
    out = []
    for d in data or []:
        L = d.get("loader") or d
        v = L.get("version")
        if v:
            out.append({"version": v, "stable": "beta" not in v.lower()})
    return out


def forge_versions(mc: str) -> List[Dict[str, Any]]:
    data = _http_json([BMCL + f"/forge/minecraft/{mc}"])
    rows = [{"version": d.get("version"), "build": d.get("build", 0),
             "modified": (d.get("modified") or "")[:10]}
            for d in data or [] if d.get("version")]
    rows.sort(key=lambda r: r["build"] or 0, reverse=True)
    return rows


def neoforge_versions(mc: str) -> List[Dict[str, Any]]:
    data = _http_json([BMCL + f"/neoforge/list/{mc}"])
    rows = [{"version": d.get("version"), "rawVersion": d.get("rawVersion", ""),
             "installerPath": d.get("installerPath", "")}
            for d in data or [] if d.get("version")]
    return rows


def list_loaders(mc: str, only: str = "") -> Dict[str, Any]:
    """列出某个 MC 版本可用的加载器版本。"""
    result: Dict[str, Any] = {"mc": mc, "loaders": {}}
    plan = [only] if only else ["fabric", "quilt", "forge", "neoforge"]
    errors: Dict[str, str] = {}
    for name in plan:
        try:
            if name == "fabric":
                result["loaders"]["fabric"] = fabric_loaders(mc)
            elif name == "quilt":
                result["loaders"]["quilt"] = quilt_loaders(mc)
            elif name == "forge":
                result["loaders"]["forge"] = forge_versions(mc)
            elif name == "neoforge":
                result["loaders"]["neoforge"] = neoforge_versions(mc)
        except Exception as e:  # noqa: BLE001
            errors[name] = str(e)[:120]
    result["errors"] = errors
    return result


def _java_exe() -> str:
    """取可用于运行安装器的 java（控制台版优先）。"""
    ji = core.resolve_java(core.load().get("javaPath", ""))
    if not ji:
        raise RuntimeError("未检测到 Java，无法运行加载器安装器")
    p = ji["path"]
    if p.lower().endswith("javaw.exe"):
        alt = p[:-9] + "java.exe"
        if os.path.isfile(alt):
            return alt
    return p


def _run_installer(jar: str, target_dir: str, mode: str = "--installClient") -> Dict[str, Any]:
    """以无界面方式运行 Forge / NeoForge 安装器。"""
    cmd = [_java_exe(), "-jar", jar, mode, target_dir]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=900,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (p.stdout or b"").decode("utf-8", "ignore")
        err = (p.stderr or b"").decode("utf-8", "ignore")
        return {"ok": p.returncode == 0, "code": p.returncode, "out": out[-2000:], "err": err[-2000:]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": -1, "out": "", "err": str(e)}


def _download_to(urls: List[str], dest: str, task=None) -> str:
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    item = DownloadItem(urls[0], dest, name=os.path.basename(dest), urls=urls)
    if core.file_ok(item):
        return dest
    download_all([item], workers=2, should_stop=(lambda: task.cancelled) if task else None)
    if not os.path.isfile(dest):
        raise RuntimeError(f"下载失败：{urls[0]}")
    return dest


def install_loader(loader: str, mc: str, loader_version: str, task) -> str:
    """
    为已安装的原版 mc 安装模组加载器，返回新版本 ID。
    loader: fabric | quilt | forge | neoforge
    """
    cfg = core.load()
    game_dir = core.game_dir(cfg)

    def _msg(s):
        if task:
            task.message = s

    # 1. 先确保原版本体可用
    _msg(f"检查原版 {mc} …")
    try:
        import game as gamemod
        if not gamemod.check_ready(mc, cfg).get("ready"):
            _msg(f"安装原版 {mc} …")
            install_version(mc, task, True)
    except ImportError:
        install_version(mc, task, True)

    before = {d for d in os.listdir(core.versions_dir(cfg))
              if os.path.isdir(os.path.join(core.versions_dir(cfg), d))}

    if loader in ("fabric", "quilt"):
        _msg(f"获取 {LOADER_NAMES[loader]} {loader_version} 配置…")
        bases = FABRIC_META if loader == "fabric" else QUILT_META
        profile = _http_json([b + f"/versions/loader/{mc}/{loader_version}/profile/json"
                              for b in bases])
        vid = profile.get("id") or f"{loader}-loader-{loader_version}-{mc}"
        vdir = core.version_dir(vid, cfg)
        os.makedirs(vdir, exist_ok=True)
        with open(os.path.join(vdir, f"{vid}.json"), "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False)

        _msg("下载加载器依赖…")
        vj = resolve_version(vid)
        items = [i for i in collect_libraries(vj, cfg) if not core.file_ok(i)]
        jar_item = collect_client_jar(vj, cfg)
        if jar_item and not core.file_ok(jar_item):
            items.append(jar_item)

        def _on_file(item, n, total):
            task.current_file = item.name
            task.total = max(1, total)
            task.done = n

        if items:
            ok, failures = download_all(
                items, workers=int(cfg.get("maxDownloadThreads", 16)),
                on_file_done=_on_file, should_stop=lambda: task.cancelled)
            if failures:
                raise RuntimeError("依赖下载失败：" + "、".join(f[0] for f in failures[:3]))
        try:
            extract_natives(vid)
        except Exception:
            pass
        return vid

    if loader == "forge":
        url = f"{BMCL}/maven/net/minecraftforge/forge/{mc}-{loader_version}/forge-{mc}-{loader_version}-installer.jar"
        urls = [url, f"https://maven.minecraftforge.net/net/minecraftforge/forge/{mc}-{loader_version}/forge-{mc}-{loader_version}-installer.jar"]
    elif loader == "neoforge":
        rows = neoforge_versions(mc)
        path = ""
        for r in rows:
            if r["version"] == loader_version:
                path = r.get("installerPath") or ""
                break
        if not path:
            path = f"/maven/net/neoforged/neoforge/{loader_version}/neoforge-{loader_version}-installer.jar"
        url = BMCL + path
        urls = [url,
                f"https://maven.neoforged.net/releases/net/neoforged/neoforge/{loader_version}/neoforge-{loader_version}-installer.jar"]
    else:
        raise RuntimeError(f"暂不支持的加载器：{loader}")

    tmp_dir = os.path.join(core.ROOT_DIR, "tmp")
    jar = os.path.join(tmp_dir, os.path.basename(url))
    _msg(f"下载 {LOADER_NAMES[loader]} 安装器…")
    _download_to(urls, jar, task)

    _msg(f"运行 {LOADER_NAMES[loader]} 安装器（约需 1-3 分钟）…")
    r = _run_installer(jar, game_dir, "--installClient")
    if not r["ok"]:
        raise RuntimeError(f"安装器执行失败（code {r['code']}）：{(r['err'] or r['out'])[-300:]}")

    after = {d for d in os.listdir(core.versions_dir(cfg))
             if os.path.isdir(os.path.join(core.versions_dir(cfg), d))}
    new = sorted(after - before)
    if not new:
        raise RuntimeError("安装器已运行，但未生成新版本目录")
    # 优先挑名字里带加载器版本号的那个
    pick = next((d for d in new if loader_version in d), new[0])
    return pick


# ==================================================================
# 服务端下载
# ==================================================================

def server_dir(vid: str, cfg: Dict[str, Any] | None = None) -> str:
    cfg = cfg or core.load()
    d = os.path.join(core.game_dir(cfg), "servers", vid)
    os.makedirs(d, exist_ok=True)
    return d


def collect_server_jar(vid: str, cfg: Dict[str, Any] | None = None) -> Optional[DownloadItem]:
    cfg = cfg or core.load()
    vj = resolve_version(vid)
    info = (vj.get("downloads") or {}).get("server") or {}
    if not info.get("url"):
        return None
    dest = os.path.join(server_dir(vid, cfg), "server.jar")
    urls = [info["url"]]
    cur = cfg.get("downloadSource", "bmclapi")
    order = [cur, "official"] if cur != "official" else ["official"]
    for sid in order:
        if sid == "bmclapi":
            u = f"{BMCL}/version/{vj.get('_jarOf') or vid}/server"
            if u not in urls:
                urls.append(u)
    return DownloadItem(urls[0], dest, size=info.get("size", 0),
                        sha1=info.get("sha1", ""), name="server.jar", urls=urls)


def install_server(vid: str, task) -> str:
    """
    下载（或生成）服务端。返回服务端目录。
    原版：直接下载 server.jar。
    加载器版：调用对应安装器的 --installServer。
    """
    cfg = core.load()
    d = server_dir(vid, cfg)

    def _msg(s):
        if task:
            task.message = s

    item = collect_server_jar(vid, cfg)
    if item:
        _msg("下载服务端 jar…")
        task.total = 1
        task.done = 0

        def _on_file(it, n, total):
            task.current_file = it.name
            task.total = total
            task.done = n

        if core.file_ok(item):
            _msg("服务端已是最新")
        else:
            ok, failures = download_all([item], workers=4, on_file_done=_on_file,
                                        should_stop=lambda: task.cancelled)
            if failures:
                raise RuntimeError("服务端下载失败：" + failures[0][1][:200])
    else:
        # 加载器版本：用安装器生成服务端
        low = vid.lower()
        loader = None
        for key in ("neoforge", "forge", "fabric", "quilt"):
            if key in low:
                loader = key
                break
        if not loader:
            raise RuntimeError("该版本没有可用的服务端下载")
        _msg(f"用 {LOADER_NAMES[loader]} 安装器生成服务端…")
        jar = _find_installer_jar(loader, vid, task)
        r = _run_installer(jar, d, "--installServer")
        if not r["ok"]:
            raise RuntimeError(f"服务端安装失败（code {r['code']}）：{(r['err'] or r['out'])[-300:]}")
        # 安装器会生成 run.bat / 或 user_jvm_args.txt 等
        _tidy_server_dir(d)

    _write_server_scripts(d, vid)
    return d


def _find_installer_jar(loader: str, vid: str, task=None) -> str:
    """为加载器版本找到（或下载）安装器 jar。"""
    cfg = core.load()
    mc = None
    cur = vid
    seen = set()
    while cur and cur not in seen:
        seen.add(cur)
        try:
            d = read_version_json(cur)
        except Exception:
            break
        if not d.get("inheritsFrom"):
            mc = cur
            break
        cur = d["inheritsFrom"]
    mc = mc or vid
    tmp = os.path.join(core.ROOT_DIR, "tmp")
    if loader == "forge":
        ver = vid.split("forge-")[-1]
        url = f"{BMCL}/maven/net/minecraftforge/forge/{mc}-{ver}/forge-{mc}-{ver}-installer.jar"
    else:
        ver = vid.split("neoforge-")[-1]
        url = f"{BMCL}/maven/net/neoforged/neoforge/{ver}/neoforge-{ver}-installer.jar"
    jar = os.path.join(tmp, os.path.basename(url))
    return _download_to([url], jar, task)


def _tidy_server_dir(d: str) -> None:
    """把安装器生成的 server jar 统一命名为 server.jar。"""
    try:
        for name in os.listdir(d):
            if name.endswith(".jar") and name != "server.jar":
                src = os.path.join(d, name)
                dst = os.path.join(d, "server.jar")
                if not os.path.isfile(dst):
                    os.replace(src, dst)
    except Exception:
        pass


def _write_server_scripts(d: str, vid: str) -> None:
    """生成 eula.txt 与启动脚本，开服即用。"""
    try:
        eula = os.path.join(d, "eula.txt")
        if not os.path.isfile(eula):
            with open(eula, "w", encoding="utf-8") as f:
                f.write("# 由脆冰启动器生成；把 false 改成 true 表示你同意 Mojang EULA\n")
                f.write("# https://aka.ms/MinecraftEULA\neula=true\n")
        bat = os.path.join(d, "启动服务端.bat")
        with open(bat, "w", encoding="utf-8") as f:
            f.write("@echo off\nchcp 65001 >nul\n")
            f.write(f'title 脆冰启动器 - {vid} 服务端\n')
            f.write('java -Xmx4G -Xms1G -jar server.jar nogui\npause\n')
    except Exception:
        pass
