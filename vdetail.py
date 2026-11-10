# -*- coding: utf-8 -*-
"""KCL 版本详情页后端支撑（v1.2.3 新增）

对应界面：版本 → 选中一个版本 →「确认并进入」→ 左侧小侧栏（版本设置 / 模组管理 / 概览）

本模块只做三块能力：
  1. 模组管理：列出（读 jar 内元数据）、启用 / 禁用、卸载、检查更新、自动兼容
  2. 版本重命名：改文件夹名（连带 .json / .jar / natives）与改显示名（不改文件夹）
  3. 目录与补全：版本 / 模组 / 存档目录定位，缺失文件检查与在线补全

约定：
  · 禁用模组 = 把 `xxx.jar` 改名为 `xxx.jar.disabled`（与主流启动器一致，游戏不会加载）
  · 模组元数据优先读 fabric.mod.json / quilt.mod.json / META-INF/mods.toml，读不到就退化用文件名
  · 更新 / 自动兼容都走 Modrinth（含镜像），按 jar 的 sha1 反查项目，再按当前 MC 版本 + 加载器挑版本
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import zipfile
from typing import Any, Dict, List, Optional

import core
import game
import mods

MOD_EXT = (".jar", ".zip", ".litemod")
DISABLED_TAIL = ".disabled"
_META_CACHE: Dict[str, Any] = {}
_SHA1_CACHE: Dict[str, Any] = {}
_CTX = threading.local()


# ==================================================================
# 小工具
# ==================================================================
def _cfg() -> Dict[str, Any]:
    return core.load()


def _mods_dir(vid: str, cfg: Dict[str, Any] | None = None) -> str:
    return mods.install_dir("mod", vid, cfg or _cfg())


def _is_mod_file(name: str) -> bool:
    low = name.lower()
    if low.endswith(DISABLED_TAIL):
        low = low[: -len(DISABLED_TAIL)]
    return low.endswith(MOD_EXT)


def _sha1(path: str) -> str:
    """按 (路径, 大小, 修改时间) 缓存，避免每次刷新都把几十 MB 的 jar 重算一遍。"""
    try:
        st = os.stat(path)
    except OSError:
        return ""
    key = (path, st.st_size, int(st.st_mtime))
    hit = _SHA1_CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    h = hashlib.sha1()
    try:
        with open(path, "rb") as fh:
            while True:
                buf = fh.read(1 << 20)
                if not buf:
                    break
                h.update(buf)
    except OSError:
        return ""
    val = h.hexdigest()
    _SHA1_CACHE[path] = (key, val)
    return val


def _read_meta(path: str) -> Dict[str, Any]:
    """从 jar 里读模组元数据。失败就返回空字段，不抛异常。"""
    try:
        st = os.stat(path)
        key = (path, st.st_size, int(st.st_mtime))
        if _META_CACHE.get(path, (None,))[0] == key:
            return _META_CACHE[path][1]
    except OSError:
        return {}
    meta: Dict[str, Any] = {"name": "", "version": "", "modId": "", "loader": "", "desc": ""}
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if "fabric.mod.json" in names or "quilt.mod.json" in names:
                raw = z.read("fabric.mod.json" if "fabric.mod.json" in names else "quilt.mod.json")
                j = json.loads(raw.decode("utf-8", "ignore"))
                if "quilt_loader" in j:                       # quilt 的 schema 多包一层
                    q = j.get("quilt_loader") or {}
                    jj = q.get("metadata") or {}
                    meta["name"] = jj.get("name") or q.get("id") or ""
                    meta["version"] = q.get("version") or ""
                    meta["modId"] = q.get("id") or ""
                else:
                    jj = j.get("name") or j.get("id") or ""
                    meta["name"] = str(jj)
                    meta["modId"] = str(j.get("id") or "")
                    meta["version"] = str(j.get("version") or "")
                    meta["desc"] = str(j.get("description") or "")
                meta["loader"] = "fabric"
            elif "META-INF/neoforge.mods.toml" in names or "META-INF/mods.toml" in names:
                f = "META-INF/neoforge.mods.toml" if "META-INF/neoforge.mods.toml" in names \
                    else "META-INF/mods.toml"
                txt = z.read(f).decode("utf-8", "ignore")

                def grab(k: str) -> str:
                    m = re.search(r'(?m)^\s*%s\s*=\s*[\'"]([^\'"]+)[\'"]' % k, txt)
                    return m.group(1) if m else ""

                meta["modId"] = grab("modId")
                meta["version"] = grab("version")
                meta["name"] = grab("displayName") or meta["modId"]
                meta["desc"] = grab("description")
                meta["loader"] = "neoforge" if "neoforge" in txt.lower() else "forge"
            elif "mcmod.info" in names:                        # 1.12 及更早
                txt = z.read("mcmod.info").decode("utf-8", "ignore")
                m = re.search(r'"name"\s*:\s*"([^"]+)"', txt)
                v = re.search(r'"version"\s*:\s*"([^"]+)"', txt)
                meta["name"] = m.group(1) if m else ""
                meta["version"] = v.group(1) if v else ""
                meta["loader"] = "forge"
    except Exception:  # noqa: BLE001 - 坏包/加密包一律降级
        meta = {"name": "", "version": "", "modId": "", "loader": "", "desc": ""}
    _META_CACHE[path] = (key, meta)
    return meta


def _mc_from_jar(vid: str) -> str:
    """从版本客户端 jar 里的 version.json 读真实 MC 版本号。

    PCL 导入的版本文档里没有 inheritsFrom，文件夹名又常被改成中文（如「红石」），
    只有 jar 内的 version.json 才是准的：{"id": "1.21.1", "name": "1.21.1"}。
    """
    jar = core.version_jar_path(vid)
    if not os.path.isfile(jar):
        return ""
    try:
        with zipfile.ZipFile(jar) as z:
            if "version.json" not in z.namelist():
                return ""
            j = json.loads(z.read("version.json").decode("utf-8", "ignore"))
            return str(j.get("id") or j.get("name") or "")
    except Exception:  # noqa: BLE001
        return ""


def _loader_from_json(vid: str) -> str:
    """从版本 json 的 libraries 判断加载器（fabric / quilt / neoforge / forge）。"""
    try:
        p = core.version_json_path(vid)
        with open(p, "r", encoding="utf-8") as fh:
            vj = json.load(fh)
    except Exception:  # noqa: BLE001
        return ""
    names = " ".join(str(l.get("name", "")).lower() for l in (vj.get("libraries") or []))
    for key, val in (("fabric-loader", "fabric"), ("quilt-loader", "quilt"),
                     ("net.neoforged", "neoforge"), ("net.minecraftforge", "forge")):
        if key in names:
            return val
    return ""


def _target(vid: str, cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """版本的目标信息：MC 版本 + 加载器（先常规推导，推导不出就深挖）。"""
    cfg = cfg or _cfg()
    info = mods.target_info(vid, cfg)
    mc = str(info.get("mc") or "")
    if not re.match(r"^\d+\.\d+", mc):
        mc = _mc_from_jar(vid) or mc
    loader = str(info.get("loader") or "")
    if not loader:
        loader = _loader_from_json(vid)
    if not loader:      # 最后兜底：看已装模组里哪种加载器最多
        d = os.path.join(core.effective_game_dir(vid, cfg), "mods")
        tally: Dict[str, int] = {}
        try:
            for name in os.listdir(d)[:40]:
                if not _is_mod_file(name):
                    continue
                base = name[: -len(DISABLED_TAIL)] if name.lower().endswith(DISABLED_TAIL) else name
                ld = (_read_meta(os.path.join(d, base)) or {}).get("loader") or ""
                if ld:
                    tally[ld] = tally.get(ld, 0) + 1
        except OSError:
            pass
        if tally:
            loader = max(tally.items(), key=lambda kv: kv[1])[0]
    return {"mc": mc, "loader": loader, "isolate": bool(info.get("isolate")),
            "dir": core.effective_game_dir(vid, cfg)}


def _fmt_size(n: int) -> str:
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB"):
        if n < step or unit == "GB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= step
    return "%.1f GB" % n


# ==================================================================
# 一、模组列表 / 启停 / 卸载
# ==================================================================
def list_mods(vid: str) -> Dict[str, Any]:
    cfg = _cfg()
    d = _mods_dir(vid, cfg)
    info = _target(vid, cfg)
    items: List[Dict[str, Any]] = []
    try:
        names = sorted(os.listdir(d), key=lambda x: x.lower())
    except OSError:
        names = []
    for name in names:
        path = os.path.join(d, name)
        if not os.path.isfile(path) or not _is_mod_file(name):
            continue
        enabled = not name.lower().endswith(DISABLED_TAIL)
        base = name[: -len(DISABLED_TAIL)] if not enabled else name
        meta = _read_meta(os.path.join(d, base)) if os.path.isfile(os.path.join(d, base)) \
            else _read_meta(path)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        items.append({
            "file": name,
            "base": base,
            "enabled": enabled,
            "size": size,
            "sizeText": _fmt_size(size),
            "name": meta.get("name") or os.path.splitext(base)[0],
            "version": meta.get("version") or "",
            "modId": meta.get("modId") or "",
            "loader": meta.get("loader") or "",
            "desc": (meta.get("desc") or "")[:180],
        })
    return {
        "dir": d,
        "mc": info.get("mc", ""),
        "loader": info.get("loader", ""),
        "isolate": bool(info.get("isolate")),
        "count": len(items),
        "enabledCount": len([m for m in items if m["enabled"]]),
        "list": items,
    }


def toggle_mod(vid: str, file: str, enabled: bool) -> Dict[str, Any]:
    d = _mods_dir(vid)
    src = os.path.join(d, os.path.basename(file))
    if not os.path.isfile(src):
        raise FileNotFoundError("模组文件不存在：" + file)
    low = src.lower()
    if enabled and low.endswith(DISABLED_TAIL):
        dst = src[: -len(DISABLED_TAIL)]
        if os.path.exists(dst):
            raise FileExistsError("同名模组已存在，无法启用")
        os.rename(src, dst)
        return {"file": os.path.basename(dst), "enabled": True}
    if not enabled and not low.endswith(DISABLED_TAIL):
        dst = src + DISABLED_TAIL
        os.rename(src, dst)
        return {"file": os.path.basename(dst), "enabled": False}
    return {"file": os.path.basename(src), "enabled": enabled}


def delete_mod(vid: str, file: str) -> Dict[str, Any]:
    d = _mods_dir(vid)
    target = os.path.join(d, os.path.basename(file))
    if not os.path.isfile(target):
        raise FileNotFoundError("模组文件不存在：" + file)
    try:
        os.remove(target)
    except OSError:
        try:                      # 只读属性等
            os.chmod(target, 0o666)
            os.remove(target)
        except OSError as e:
            raise RuntimeError("删除失败：%s" % e) from e
    return {"file": os.path.basename(file), "deleted": True}


# ==================================================================
# 二、检查更新 / 自动兼容（Modrinth）
# ==================================================================
def _mr_headers() -> Dict[str, str]:
    return {"User-Agent": mods.UA, "Content-Type": "application/json"}


def _mr_hash_lookup(hashes: List[str], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """按 sha1 批量反查 Modrinth 版本对象：{hash: version_obj}。"""
    if not hashes:
        return {}
    import requests
    url = mods.mr_base(cfg) + "/version_files"
    try:
        r = requests.post(url, data=json.dumps({"hashes": hashes, "algorithm": "sha1"}),
                          headers=_mr_headers(), timeout=(8, 25))
        if r.status_code == 200:
            return r.json() or {}
    except Exception:  # noqa: BLE001
        return {}
    return {}


def _mr_version(version_id: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """取单个版本的详情（含 dependencies，用于「自动兼容」判断前置）。"""
    if not version_id:
        return {}
    try:
        return mods._get(mods.mr_base(cfg) + "/version/" + version_id) or {}
    except Exception:  # noqa: BLE001
        return {}


def check_updates(vid: str, files: Optional[List[str]] = None) -> Dict[str, Any]:
    """检查每个模组在 Modrinth 上有没有「兼容当前 MC 版本 + 加载器」的新版本。"""
    cfg = _cfg()
    data = list_mods(vid)
    d = data["dir"]
    mc, loader = data["mc"], data["loader"]
    want = set(files or [])
    targets = [m for m in data["list"] if (not want or m["file"] in want or m["base"] in want)]

    hash_map = {m["file"]: _sha1(os.path.join(d, m["file"])) for m in targets}
    lookup = _mr_hash_lookup([h for h in hash_map.values() if h], cfg)

    proj_cache: Dict[str, List[Dict[str, Any]]] = {}
    items: List[Dict[str, Any]] = []
    for m in targets:
        h = hash_map.get(m["file"], "")
        cur = lookup.get(h) or {}
        pid = cur.get("project_id") or ""
        row = {
            "file": m["file"], "base": m["base"], "name": m["name"],
            "current": m["version"] or (cur.get("version_number") or ""),
            "projectId": pid, "loader": m["loader"] or loader,
            "latest": None, "reason": "",
        }
        if not pid:
            row["reason"] = "平台未收录（无法判断更新）"
            items.append(row)
            continue
        if pid not in proj_cache:
            try:
                proj_cache[pid] = mods.versions_modrinth(pid, mc, loader, cfg)
            except Exception:  # noqa: BLE001
                proj_cache[pid] = []
        cands = proj_cache[pid]
        # 接口返回顺序不保证按时间，这里自己按发布日期倒序（否则会把新版误判成「可更新到旧版」）
        cands = sorted(cands, key=lambda c: (c.get("date") or ""), reverse=True)
        proj_cache[pid] = cands
        newest = cands[0] if cands else None
        cur_date = str(cur.get("date_published") or "")[:10]
        if not cands:
            row["reason"] = "该 MC 版本/加载器下没有可用文件"
        elif not newest or newest.get("id") == cur.get("id"):
            row["reason"] = "已是最新"
        elif cur_date and (newest.get("date") or "") <= cur_date:
            row["reason"] = "已是最新"
        else:
            row["latest"] = newest
            row["reason"] = "可更新 → %s" % (newest.get("version") or newest.get("name") or "")
        items.append(row)
    upd = [i for i in items if i["latest"]]
    return {"mc": mc, "loader": loader, "count": len(items),
            "updateCount": len(upd), "items": items}


def _pick_compatible(vid: str, pid: str, cands: List[Dict[str, Any]],
                     installed: Dict[str, str], cfg: Dict[str, Any]) -> tuple:
    """在一串候选版本里挑「前置都满足」的那个（自动兼容的核心）。

    判据：候选版本的 required 依赖项目，必须已经装在当前版本里（或者没有 project_id 的外部前置）。
    返回 (候选, 说明)。
    """
    for c in cands:
        detail = _mr_version(c.get("id") or "", cfg)
        deps = detail.get("dependencies") or []
        need = [d for d in deps if d.get("dependency_type") == "required"]
        missing = [d for d in need
                   if d.get("project_id") and d["project_id"] not in installed]
        if not missing:
            return c, ("前置齐备" if need else "无前置要求")
    if cands:
        return cands[0], "前置不全，退回该 MC 版本最新版"
    return None, "无可用版本"


def auto_compat(vid: str, apply: bool = True) -> Dict[str, Any]:
    """自动兼容：把所有可更新模组换成「与前置兼容」的版本（可只出方案不落地）。"""
    cfg = _cfg()
    base = check_updates(vid)
    d = _mods_dir(vid, cfg)

    # 已装项目集合：用于判断候选版本的前置是否齐备
    installed: Dict[str, str] = {}
    try:
        hashes = [_sha1(os.path.join(d, m["file"])) for m in base["items"]]
        lookup = _mr_hash_lookup([h for h in hashes if h], cfg)
        for h, v in lookup.items():
            pid = (v or {}).get("project_id")
            if pid:
                installed[pid] = v.get("id") or ""
    except Exception:  # noqa: BLE001
        pass

    plan: List[Dict[str, Any]] = []
    for item in base["items"]:
        if not item.get("latest"):
            continue
        pid = item["projectId"]
        try:
            cands = mods.versions_modrinth(pid, base["mc"], base["loader"], cfg)
        except Exception:  # noqa: BLE001
            cands = [item["latest"]]
        cands = sorted(cands, key=lambda c: (c.get("date") or ""), reverse=True)
        pick, why = _pick_compatible(vid, pid, cands, installed, cfg)
        if pick:
            plan.append({"file": item["file"], "base": item["base"], "name": item["name"],
                         "from": item["current"], "to": pick.get("version", ""),
                         "fileName": pick.get("fileName"), "url": pick.get("url"),
                         "why": why})
    if not apply or not plan:
        return {"planned": plan, "applied": [], "failed": [], "apply": bool(apply)}
    res = _apply_plan(vid, plan)
    return {"planned": plan, "apply": True, **res}


def update_mods(vid: str, files: Optional[List[str]] = None) -> Dict[str, Any]:
    """更新指定（或全部可更新）模组到最新兼容版本。"""
    chk = check_updates(vid, files)
    plan = []
    for it in chk["items"]:
        latest = it.get("latest")
        if not latest:
            continue
        plan.append({"file": it["file"], "base": it["base"], "name": it["name"],
                     "from": it["current"], "to": latest.get("version", ""),
                     "fileName": latest.get("fileName"), "url": latest.get("url"),
                     "why": "最新兼容版"})
    if not plan:
        return {"planned": [], "applied": [], "failed": [], "apply": True}
    return {"planned": plan, "apply": True, **_apply_plan(vid, plan)}


def _apply_plan(vid: str, plan: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按计划下载新文件、替换旧文件（保留启用/禁用状态）。"""
    cfg = _cfg()
    d = _mods_dir(vid, cfg)
    applied, failed = [], []
    for p in plan:
        url, fname = p.get("url"), p.get("fileName")
        old_enabled = not p["file"].lower().endswith(DISABLED_TAIL)
        if not url or not fname:
            failed.append({"file": p["file"], "msg": "缺少下载地址"})
            continue
        dest = os.path.join(d, fname)
        tmp = dest + ".cbtmp"
        try:
            import requests
            with requests.get(url, headers={"User-Agent": mods.UA}, stream=True,
                              timeout=(10, 120)) as r:
                r.raise_for_status()
                os.makedirs(d, exist_ok=True)
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_content(1 << 16):
                        if chunk:
                            fh.write(chunk)
            os.replace(tmp, dest)
            if not old_enabled:
                os.replace(dest, dest + DISABLED_TAIL)
            old = os.path.join(d, p["file"])
            if os.path.isfile(old) and os.path.abspath(old) != os.path.abspath(
                    dest + ("" if old_enabled else DISABLED_TAIL)):
                try:
                    os.remove(old)
                except OSError:
                    pass
            applied.append({"name": p["name"], "from": p["from"], "to": p["to"], "file": fname})
        except Exception as e:  # noqa: BLE001
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            failed.append({"file": p["file"], "msg": str(e)[:160]})
    return {"applied": applied, "failed": failed}


# ==================================================================
# 三、版本重命名（文件夹名 / 显示名）
# ==================================================================
_NAME_BAD = re.compile(r'[\\/:*?"<>|]')


def rename_version(vid: str, new_id: str = "", display_name: str = "") -> Dict[str, Any]:
    """改版本。

    · new_id 非空且不同 -> 改版本文件夹名（连带 <id>.json / <id>.jar / <id>-natives），
      并迁移版本设置、上次选择等配置
    · display_name 非空 -> 只改启动器里显示的名字，文件夹不动
    """
    cfg = _cfg()
    vdir_root = core.versions_dir(cfg)
    old_dir = core.version_dir(vid, cfg)
    if not os.path.isdir(old_dir):
        raise FileNotFoundError("版本不存在：" + vid)

    new_id = (new_id or "").strip()
    result = {"id": vid, "displayName": (display_name or "").strip(), "renamed": False}

    if new_id and new_id != vid:
        if _NAME_BAD.search(new_id) or new_id in (".", ".."):
            raise ValueError("版本名不能包含 \\ / : * ? \" < > |")
        new_dir = os.path.join(vdir_root, new_id)
        if os.path.exists(new_dir):
            raise FileExistsError("已存在同名版本：" + new_id)
        os.rename(old_dir, new_dir)
        for suffix in (".json", ".jar"):
            src = os.path.join(new_dir, vid + suffix)
            if os.path.isfile(src):
                os.replace(src, os.path.join(new_dir, new_id + suffix))
        src_nat = os.path.join(new_dir, vid + "-natives")
        if os.path.isdir(src_nat):
            os.replace(src_nat, os.path.join(new_dir, new_id + "-natives"))
        # 版本 json 里的 id 也要跟着改，否则加载器解析会认错
        jf = os.path.join(new_dir, new_id + ".json")
        if os.path.isfile(jf):
            try:
                with open(jf, "r", encoding="utf-8") as fh:
                    vj = json.load(fh)
                if vj.get("id") == vid:
                    vj["id"] = new_id
                    for lib_owner in ("jar",):
                        if isinstance(vj.get(lib_owner), str) and vj[lib_owner] == vid:
                            vj[lib_owner] = new_id
                    with open(jf, "w", encoding="utf-8") as fh:
                        json.dump(vj, fh, ensure_ascii=False, indent=2)
            except Exception:  # noqa: BLE001
                pass
        # 配置迁移
        vs = cfg.get("versionSettings") or {}
        if vid in vs:
            vs[new_id] = vs.pop(vid)
            cfg["versionSettings"] = vs
        names = cfg.get("versionNames") or {}
        if vid in names:
            names[new_id] = names.pop(vid)
        cfg["versionNames"] = names
        if cfg.get("lastVersion") == vid:
            cfg["lastVersion"] = new_id
        result.update({"id": new_id, "renamed": True, "oldId": vid})

    if result["displayName"]:
        names = cfg.get("versionNames") or {}
        names[result["id"]] = result["displayName"]
        cfg["versionNames"] = names
    core.save(cfg)
    return result


def display_names() -> Dict[str, str]:
    return dict(core.load().get("versionNames") or {})


# ==================================================================
# 四、目录定位 / 缺失文件 / 补全
# ==================================================================
def dirs_of(vid: str) -> Dict[str, Any]:
    cfg = _cfg()
    gdir = core.effective_game_dir(vid, cfg)
    info = _target(vid, cfg)
    return {
        "version": core.version_dir(vid, cfg),
        "mods": os.path.join(gdir, "mods"),
        "saves": os.path.join(gdir, "saves"),
        "game": gdir,
        "mc": info.get("mc", ""),
        "loader": info.get("loader", ""),
        "isolate": bool(info.get("isolate")),
    }


def missing(vid: str) -> Dict[str, Any]:
    try:
        miss, warns = game.missing_files(vid, core.load(), check_assets=False)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": str(e), "missing": [], "count": 0, "warnings": []}
    return {"ok": True, "count": len(miss), "missing": miss[:40], "warnings": warns[:10]}


def repair(vid: str) -> Dict[str, Any]:
    """补全文件：后台线程跑，界面轮询 /api/tasks 看进度。"""
    st = missing(vid)
    if not st.get("ok"):
        raise RuntimeError(st.get("msg") or "检查失败")
    threading.Thread(target=game._repair_files, args=(vid, True), daemon=True).start()
    return {"started": True, "count": st["count"], "missing": st["missing"]}
