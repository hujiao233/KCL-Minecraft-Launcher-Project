# -*- coding: utf-8 -*-
"""
脆冰启动器 - 存档与地图
1) 读取存档列表、seed、游戏版本
2) 解析 Anvil (.mca) 区块，渲染实时俯视地图
3) 从区块结构数据读取真实结构位置；无数据时按 seed 推测
4) 史莱姆区块（精确算法）
5) 标点 / 规划（保存为 JSON）
"""
from __future__ import annotations

import glob
import json
import math
import os
import struct
import time
import zlib
from typing import Any, Dict, List, Optional, Tuple

import core
import version as vmod

# ==================================================================
# NBT 读取（精简实现，够用即可）
# ==================================================================

_TAG_NAMES = {0: "end", 1: "byte", 2: "short", 3: "int", 4: "long", 5: "float",
              6: "double", 7: "byte_array", 8: "string", 9: "list", 10: "compound",
              11: "int_array", 12: "long_array"}


class _NBT:
    def __init__(self, buf: bytes):
        self.b = buf
        self.i = 0

    def u1(self) -> int:
        v = self.b[self.i]; self.i += 1; return v

    def i1(self) -> int:
        v = self.b[self.i]; self.i += 1
        return v - 256 if v > 127 else v

    def u2(self) -> int:
        v = struct.unpack_from(">H", self.b, self.i)[0]; self.i += 2; return v

    def i2(self) -> int:
        return struct.unpack_from(">h", self.b, self.i - 0)[0] if False else self._su(">h", 2)

    def i4(self) -> int:
        return self._su(">i", 4)

    def i8(self) -> int:
        return self._su(">q", 8)

    def f4(self) -> float:
        v = struct.unpack_from(">f", self.b, self.i)[0]; self.i += 4; return v

    def f8(self) -> float:
        v = struct.unpack_from(">d", self.b, self.i)[0]; self.i += 8; return v

    def _su(self, fmt: str, size: int) -> int:
        v = struct.unpack_from(fmt, self.b, self.i)[0]; self.i += size; return v

    def raw(self, n: int) -> bytes:
        v = self.b[self.i:self.i + n]; self.i += n; return v

    def string(self) -> str:
        n = self.u2()
        return self.raw(n).decode("utf-8", errors="replace")

    def payload(self, t: int) -> Any:
        if t == 1: return self.i1()
        if t == 2: return self._su(">h", 2)
        if t == 3: return self._su(">i", 4)
        if t == 4: return self._su(">q", 8)
        if t == 5: return self.f4()
        if t == 6: return self.f8()
        if t == 7: return self.raw(self._su(">i", 4))
        if t == 8: return self.string()
        if t == 9:
            et = self.u1(); n = self._su(">i", 4)
            out = []
            for _ in range(n):
                out.append(self.payload(et) if et != 0 else None)
            return out
        if t == 10:
            out: Dict[str, Any] = {}
            while True:
                et = self.u1()
                if et == 0:
                    break
                name = self.string()
                out[name] = self.payload(et)
            return out
        if t == 11:
            n = self._su(">i", 4)
            return list(struct.unpack_from(">%di" % n, self.b, self.i)) if n else []
        if t == 12:
            n = self._su(">i", 4)
            return list(struct.unpack_from(">%dq" % n, self.b, self.i)) if n else []
        raise ValueError("未知 NBT 类型 " + str(t))


def parse_nbt(buf: bytes) -> Tuple[str, Any]:
    """返回 (根标签名, 值)。"""
    r = _NBT(buf)
    t = r.u1()
    if t == 0:
        return ("", None)
    name = r.string()
    return (name, r.payload(t))


def _decompress(data: bytes) -> bytes:
    if data[:2] == b"\x1f\x8b":
        import gzip
        return gzip.decompress(data)
    return zlib.decompress(data)


def read_level_dat(path: str) -> Dict[str, Any]:
    _, root = parse_nbt(_decompress(open(path, "rb").read()))
    return (root or {}).get("Data", {}) or {}


# ==================================================================
# Java Random（史莱姆区块等算法需要）
# ==================================================================

MASK48 = (1 << 48) - 1


def _i32(x: int) -> int:
    x &= 0xFFFFFFFF
    return x - (1 << 32) if x >= (1 << 31) else x


class JavaRandom:
    __slots__ = ("seed",)

    def __init__(self, seed: int):
        self.seed = (_i32(seed) ^ 0x5DEECE66D) & MASK48

    def next(self, bits: int) -> int:
        self.seed = (self.seed * 0x5DEECE66D + 0xB) & MASK48
        return self.seed >> (48 - bits)

    def next_int(self, n: Optional[int] = None) -> int:
        if n is None:
            return _i32(self.next(32))
        if n <= 0:
            raise ValueError("bound must be positive")
        if (n & (n - 1)) == 0:            # 2 的幂，走快速路径
            return (n * self.next(31)) >> 31
        while True:
            bits = self.next(31)
            val = bits % n
            if bits - val + (n - 1) < (1 << 31):
                return val


def is_slime_chunk(seed: int, cx: int, cz: int) -> bool:
    """Java 版史莱姆区块判定（精确）。"""
    s = seed + cx * cx * 4987142 + cx * 5947611 + cz * cz * 4392871 + cz * 389711
    return JavaRandom(s ^ 987234911).next_int(10) == 0


# ==================================================================
# 存档
# ==================================================================

def saves_dir() -> str:
    cfg = core.load()
    base = cfg.get("gameDir") or core.DEFAULT_GAME_DIR
    return os.path.join(base, "saves")


def _all_save_dirs() -> List[str]:
    """包含版本隔离目录下的所有 saves。"""
    cfg = core.load()
    roots = [saves_dir()]
    base = cfg.get("gameDir") or core.DEFAULT_GAME_DIR
    vdir = os.path.join(base, "versions")
    if os.path.isdir(vdir):
        for name in os.listdir(vdir):
            p = os.path.join(vdir, name, "saves")
            if os.path.isdir(p):
                roots.append(p)
    try:
        for v in vmod.list_installed():
            p = os.path.join(core.effective_game_dir(v["id"], cfg), "saves")
            if os.path.isdir(p):
                roots.append(p)
    except Exception:
        pass
    seen, out = set(), []
    for r in roots:
        r = os.path.normpath(r)
        if r.lower() in seen or not os.path.isdir(r):
            continue
        seen.add(r.lower())
        out.append(r)
    return out


def list_worlds() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for root in _all_save_dirs():
        try:
            for name in sorted(os.listdir(root)):
                d = os.path.join(root, name)
                lvl = os.path.join(d, "level.dat")
                if not os.path.isfile(lvl):
                    continue
                try:
                    data = read_level_dat(lvl)
                except Exception:
                    continue
                seed = _extract_seed(data)
                out.append({
                    "name": data.get("LevelName") or name,
                    "dir": d,
                    "folder": name,
                    "seed": seed,
                    "version": str((data.get("Version") or {}).get("Name", "")
                                   if isinstance(data.get("Version"), dict)
                                   else (data.get("Version") or "")),
                    "gameType": data.get("GameType", 0),
                    "size": _world_size(d),
                    "lastPlayed": int(data.get("LastPlayed", 0) or 0),
                    "time": time.strftime("%Y-%m-%d %H:%M",
                                         time.localtime(int(data.get("LastPlayed", 0) or 0) / 1000))
                            if data.get("LastPlayed") else "",
                    "hardcore": bool(data.get("hardcore", False)),
                })
        except Exception:
            continue
    out.sort(key=lambda x: x.get("lastPlayed") or 0, reverse=True)
    return out


def _extract_seed(data: Dict[str, Any]) -> Optional[int]:
    # 1.18+: Data.WorldGenSettings.seed
    wgs = data.get("WorldGenSettings")
    if isinstance(wgs, dict) and "seed" in wgs:
        return int(wgs["seed"])
    # 1.16~1.17
    for k in ("RandomSeed", "Seed"):
        if k in data:
            return int(data[k])
    return None


def _world_size(d: str) -> int:
    total = 0
    for sub in ("region", "DIM-1/region", "DIM1/region"):
        p = os.path.join(d, sub)
        if os.path.isdir(p):
            total += len([f for f in os.listdir(p) if f.endswith(".mca")])
    return total * 512 * 512   # 粗略：每个区域文件覆盖 512x512 格


# ==================================================================
# Anvil 区块读取与渲染
# ==================================================================

SECTOR = 4096


class Region:
    """一个 .mca 区域文件（32x32 区块）。"""

    def __init__(self, path: str):
        self.path = path
        self.f = open(path, "rb")
        self.entries: List[Tuple[int, int]] = []
        head = self.f.read(SECTOR)
        if len(head) < SECTOR:
            self.entries = [(0, 0)] * 1024      # 空/损坏的区域文件
            return
        for i in range(1024):
            off = (head[i * 4] << 16) | (head[i * 4 + 1] << 8) | head[i * 4 + 2]
            cnt = head[i * 4 + 3]
            self.entries.append((off, cnt))

    def chunk(self, cx: int, cz: int) -> Optional[Dict[str, Any]]:
        i = (cx & 31) + (cz & 31) * 32
        off, cnt = self.entries[i]
        if off == 0 or cnt == 0:
            return None
        try:
            self.f.seek(off * SECTOR)
            raw = self.f.read(cnt * SECTOR)
            n = struct.unpack_from(">I", raw, 0)[0]
            comp = raw[4]
            payload = raw[5:5 + n - 1]
            data = zlib.decompress(payload) if comp == 2 else \
                (_decompress(payload) if comp == 1 else payload)
            _, root = parse_nbt(data)
            return root
        except Exception:
            return None

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass


# 常见方块颜色（精简表，未命中的按名字哈希）
BLOCK_COLORS = {
    "minecraft:air": None, "minecraft:cave_air": None, "minecraft:void_air": None,
    "minecraft:grass_block": (0x7C, 0xA6, 0x55), "minecraft:grass": (0x7C, 0xA6, 0x55),
    "minecraft:dirt": (0x8B, 0x65, 0x43), "minecraft:coarse_dirt": (0x7A, 0x56, 0x38),
    "minecraft:podzol": (0x57, 0x3D, 0x22), "minecraft:mycelium": (0x77, 0x6B, 0x63),
    "minecraft:stone": (0x8A, 0x8A, 0x8A), "minecraft:andesite": (0x8C, 0x8C, 0x8C),
    "minecraft:diorite": (0xBD, 0xBD, 0xBD), "minecraft:granite": (0x95, 0x6A, 0x55),
    "minecraft:deepslate": (0x57, 0x57, 0x5C), "minecraft:tuff": (0x6B, 0x6E, 0x60),
    "minecraft:cobblestone": (0x7A, 0x7A, 0x7A), "minecraft:mossy_cobblestone": (0x6E, 0x7A, 0x5C),
    "minecraft:stone_bricks": (0x7C, 0x7C, 0x7C), "minecraft:bedrock": (0x53, 0x53, 0x53),
    "minecraft:sand": (0xE0, 0xD8, 0xA8), "minecraft:red_sand": (0xBD, 0x72, 0x39),
    "minecraft:sandstone": (0xDF, 0xD6, 0xA5), "minecraft:gravel": (0x84, 0x7E, 0x7E),
    "minecraft:clay": (0xA0, 0xA6, 0xB4), "minecraft:snow_block": (0xF2, 0xFA, 0xFF),
    "minecraft:snow": (0xF6, 0xFC, 0xFF), "minecraft:ice": (0x9E, 0xC8, 0xED),
    "minecraft:packed_ice": (0x7E, 0xA8, 0xD0), "minecraft:blue_ice": (0x74, 0x9E, 0xD8),
    "minecraft:water": (0x3B, 0x6E, 0xC8), "minecraft:lava": (0xE0, 0x6A, 0x1E),
    "minecraft:oak_leaves": (0x4E, 0x7A, 0x2E), "minecraft:birch_leaves": (0x6B, 0x8A, 0x3E),
    "minecraft:spruce_leaves": (0x3E, 0x63, 0x36), "minecraft:dark_oak_leaves": (0x3C, 0x6A, 0x24),
    "minecraft:acacia_leaves": (0x5E, 0x7E, 0x2E), "minecraft:jungle_leaves": (0x3E, 0x7A, 0x2A),
    "minecraft:oak_log": (0x6B, 0x51, 0x2E), "minecraft:birch_log": (0xC8, 0xB8, 0x8A),
    "minecraft:spruce_log": (0x4A, 0x33, 0x1C), "minecraft:dark_oak_log": (0x3E, 0x2A, 0x16),
    "minecraft:oak_planks": (0xA8, 0x82, 0x4E), "minecraft:birch_planks": (0xD0, 0xBE, 0x8E),
    "minecraft:spruce_planks": (0x7B, 0x5A, 0x32), "minecraft:dark_oak_planks": (0x4A, 0x31, 0x1B),
    "minecraft:acacia_planks": (0xB4, 0x6A, 0x3C), "minecraft:jungle_planks": (0xA0, 0x74, 0x4A),
    "minecraft:oak_wood": (0xA8, 0x82, 0x4E), "minecraft:crafting_table": (0x7A, 0x5A, 0x34),
    "minecraft:chest": (0xA0, 0x74, 0x2C), "minecraft:furnace": (0x7E, 0x7E, 0x7E),
    "minecraft:glass": (0xC8, 0xEC, 0xF2), "minecraft:glowstone": (0xF0, 0xDA, 0x8A),
    "minecraft:torch": (0xF0, 0xC0, 0x40), "minecraft:coal_ore": (0x5A, 0x5A, 0x5A),
    "minecraft:iron_ore": (0xB0, 0x88, 0x6E), "minecraft:gold_ore": (0xDA, 0xC0, 0x44),
    "minecraft:diamond_ore": (0x5E, 0xD8, 0xD0), "minecraft:emerald_ore": (0x3E, 0xD0, 0x6A),
    "minecraft:redstone_ore": (0xC0, 0x3A, 0x2A), "minecraft:lapis_ore": (0x3A, 0x5E, 0xA8),
    "minecraft:netherrack": (0x8E, 0x3A, 0x3A), "minecraft:soul_sand": (0x5A, 0x3E, 0x2E),
    "minecraft:nether_bricks": (0x3A, 0x1E, 0x22), "minecraft:basalt": (0x4A, 0x46, 0x4E),
    "minecraft:blackstone": (0x2E, 0x28, 0x2E), "minecraft:crimson_nylium": (0x8A, 0x2C, 0x3C),
    "minecraft:warped_nylium": (0x2C, 0x6E, 0x6A), "minecraft:magma_block": (0xA8, 0x4A, 0x1E),
    "minecraft:end_stone": (0xDC, 0xDF, 0xA0), "minecraft:purpur_block": (0xA8, 0x7A, 0xA8),
    "minecraft:obsidian": (0x14, 0x0E, 0x24), "minecraft:farmland": (0x5A, 0x3E, 0x24),
    "minecraft:wheat": (0xC8, 0xB4, 0x3C), "minecraft:carrots": (0x6E, 0x9A, 0x2E),
    "minecraft:potatoes": (0x6E, 0x9A, 0x2E), "minecraft:melon": (0x7A, 0x9A, 0x2E),
    "minecraft:pumpkin": (0xC0, 0x7A, 0x1E), "minecraft:hay_block": (0xC0, 0xA0, 0x3C),
    "minecraft:bricks": (0x9E, 0x5A, 0x44), "minecraft:wool": (0xE8, 0xE8, 0xE8),
    "minecraft:white_wool": (0xEE, 0xEE, 0xEE), "minecraft:red_wool": (0xA8, 0x32, 0x32),
    "minecraft:blue_wool": (0x32, 0x4A, 0xA8), "minecraft:concrete": (0x9E, 0x9E, 0x9E),
    "minecraft:terracotta": (0xA0, 0x5E, 0x3E), "minecraft:white_terracotta": (0xD8, 0xC0, 0xAC),
    "minecraft:sponge": (0xC8, 0xC0, 0x3C), "minecraft:slime_block": (0x6E, 0xC8, 0x4A),
    "minecraft:honey_block": (0xE0, 0xB0, 0x3C), "minecraft:ladder": (0x8A, 0x6A, 0x3E),
    "minecraft:bookshelf": (0xA0, 0x74, 0x3C), "minecraft:enchanting_table": (0x8A, 0x3A, 0x3A),
    "minecraft:anvil": (0x4A, 0x4A, 0x4A), "minecraft:beacon": (0x7A, 0xE0, 0xD0),
    "minecraft:cobweb": (0xE0, 0xE0, 0xE0), "minecraft:sea_lantern": (0xA8, 0xD8, 0xD0),
    "minecraft:prismarine": (0x6E, 0xA8, 0x9A), "minecraft:dark_prismarine": (0x3A, 0x5E, 0x56),
    "minecraft:kelp": (0x3E, 0x7A, 0x3A), "minecraft:seagrass": (0x3E, 0x8A, 0x3E),
    "minecraft:lily_pad": (0x3E, 0x7A, 0x2E), "minecraft:cactus": (0x3E, 0x7A, 0x2E),
    "minecraft:vine": (0x4A, 0x7A, 0x2E), "minecraft:bamboo": (0x6E, 0x9E, 0x3E),
    "minecraft:amethyst_block": (0x9A, 0x6E, 0xC8), "minecraft:copper_block": (0xC0, 0x7A, 0x5A),
    "minecraft:dripstone_block": (0x8A, 0x72, 0x60), "minecraft:moss_block": (0x5E, 0x8A, 0x3E),
    "minecraft:deepslate_tile": (0x4A, 0x4A, 0x4E), "minecraft:sculk": (0x1A, 0x2A, 0x2E),
}


def block_color(name: str) -> Tuple[int, int, int]:
    c = BLOCK_COLORS.get(name)
    if c:
        return c
    # 未收录的方块：按名字生成稳定颜色
    h = 0
    for ch in name:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return (90 + (h % 130), 80 + ((h >> 8) % 130), 80 + ((h >> 16) % 130))


def _unpack_long_array(data: List[int], bits: int, count: int) -> List[int]:
    """把 long[] 按 bits 位解包（Minecraft 1.16+ 方块存储）。"""
    out = [0] * count
    mask = (1 << bits) - 1
    per = 64 // bits if bits else 0
    if bits == 0:
        return out
    idx = 0
    for v in data:
        v &= 0xFFFFFFFFFFFFFFFF
        for k in range(per):
            if idx >= count:
                return out
            out[idx] = (v >> (k * bits)) & mask
            idx += 1
    return out


AIR = ("minecraft:air", "minecraft:cave_air", "minecraft:void_air")


def _level_of(root: Dict[str, Any]) -> Dict[str, Any]:
    """
    兼容两种区块布局：
    1.18+  -> {sections, Heightmaps, structures, ...}
    旧版   -> {Level: {Sections, HeightMap, Structures, ...}}
    """
    if isinstance(root, dict) and isinstance(root.get("Level"), dict):
        return root["Level"]
    return root or {}


class ChunkPixels:
    """从区块 NBT 取出 16x16 的顶层方块与高度。"""

    @staticmethod
    def render(root: Dict[str, Any]) -> Optional[Tuple[List[str], List[int]]]:
        try:
            lv = _level_of(root)
            sections = lv.get("sections") or lv.get("Sections") or []
            if not sections:
                return None

            secs: Dict[int, Tuple[List[str], Optional[List[int]]]] = {}
            for s in sections:
                if not isinstance(s, dict):
                    continue
                y = s.get("Y", s.get("y"))
                if y is None:
                    continue
                bs = s.get("block_states") or s.get("BlockStates") or s.get("blockStates")
                if not isinstance(bs, dict):
                    continue
                pal = bs.get("palette") or bs.get("Palette") or []
                if not pal:
                    continue
                names = [((p.get("Name") if isinstance(p, dict) else p) or "minecraft:air")
                         for p in pal]
                # 整段都是空气，跳过（省时）
                if all(n in AIR for n in names):
                    continue
                d = bs.get("data") if "data" in bs else bs.get("Data")
                if d is None:
                    # Minecraft 优化：palette 只有一种方块时不写 data
                    idxs = None if len(names) == 1 else None
                    if len(names) != 1:
                        continue
                elif isinstance(d, list) and d and isinstance(d[0], int):
                    bits = max(4, (len(names) - 1).bit_length())
                    idxs = _unpack_long_array(d, bits, 4096)
                else:
                    continue
                secs[int(y)] = (names, idxs)
            if not secs:
                return None

            # 高度图（可选）：1.16+ 是 long[37]，每 9 位一格
            heights: Optional[List[int]] = None
            hm = (lv.get("Heightmaps") or lv.get("HeightMap") or {})
            if isinstance(hm, dict):
                h = hm.get("WORLD_SURFACE") or hm.get("WORLD_SURFACE_WG")
                if isinstance(h, list) and len(h) == 37:
                    heights = _unpack_long_array(h, 9, 256)
                elif isinstance(h, list) and len(h) == 256:
                    heights = [int(x) for x in h]

            order = sorted(secs.keys(), reverse=True)
            names_out: List[str] = []
            y_out: List[int] = []
            for z in range(16):
                for x in range(16):
                    i = z * 16 + x
                    nm = "minecraft:air"
                    fy = order[0] * 16
                    if heights is not None:
                        top = int(heights[i]) - 1
                        sy = top // 16
                        ly = top - sy * 16
                        # 从高度图指示的位置开始往下找第一个非空气
                        start = None
                        for k, s in enumerate(order):
                            if s <= sy:
                                start = k
                                break
                        if start is None:
                            start = 0
                        for s in order[start:]:
                            names, idxs = secs[s]
                            hi = 15 if s < sy else ly
                            for l in range(hi, -1, -1):
                                bi = l * 256 + z * 16 + x
                                pi = 0 if idxs is None else idxs[bi]
                                cand = names[pi] if pi < len(names) else "minecraft:air"
                                if cand not in AIR:
                                    nm, fy = cand, s * 16 + l
                                    break
                            if nm not in AIR:
                                break
                    else:
                        # 没有高度图：从上往下扫
                        for s in order:
                            names, idxs = secs[s]
                            if idxs is None:
                                if names[0] not in AIR:
                                    nm, fy = names[0], s * 16 + 15
                                    break
                                continue
                            hit = False
                            for l in range(15, -1, -1):
                                bi = l * 256 + z * 16 + x
                                pi = idxs[bi] if bi < len(idxs) else 0
                                cand = names[pi] if pi < len(names) else "minecraft:air"
                                if cand not in AIR:
                                    nm, fy = cand, s * 16 + l
                                    hit = True
                                    break
                            if hit:
                                break
                    names_out.append(nm)
                    y_out.append(fy)
            return (names_out, y_out)
        except Exception:
            return None


def region_files(world_dir: str, dim: str = "overworld") -> Dict[Tuple[int, int], str]:
    sub = {"overworld": "region", "nether": "DIM-1/region", "end": "DIM1/region"}[dim]
    p = os.path.join(world_dir, sub)
    out: Dict[Tuple[int, int], str] = {}
    if not os.path.isdir(p):
        return out
    for f in os.listdir(p):
        if not f.startswith("r.") or not f.endswith(".mca"):
            continue
        try:
            _, xs, zs, _ = f.split(".")
            out[(int(xs), int(zs))] = os.path.join(p, f)
        except Exception:
            continue
    return out


def render_map(world_dir: str, dim: str = "overworld",
               center: Tuple[float, float] = (0, 0), span: int = 512,
               tile: int = 256, marks: bool = True) -> Dict[str, Any]:
    """
    渲染一张俯视地图。
    center: (世界 X, Z)；span: 覆盖的格数；tile: 输出像素边长
    返回 PNG(base64) 与覆盖范围。
    """
    regs = region_files(world_dir, dim)
    if not regs:
        return {"ok": False, "msg": "这个存档还没有可渲染的区域文件"}
    cx, cz = center
    half = span / 2.0
    x0, z0 = cx - half, cz - half
    px_per_block = tile / float(span)
    img = bytearray(b"\x00" * (tile * tile * 3))
    filled = 0

    # 逐像素采样一次（简单、够用）
    for py in range(tile):
        wz = int(z0 + py / px_per_block)
        for px_ in range(tile):
            wx = int(x0 + px_ / px_per_block)
            rx, rz = wx >> 9, wz >> 9          # 区域坐标（512 格）
            path = regs.get((rx, rz))
            if not path:
                continue
            ccx, ccz = (wx >> 4) & 31, (wz >> 4) & 31
            try:
                reg = _REGION_CACHE.get(path)
                if reg is None:
                    reg = Region(path)
                    _REGION_CACHE[path] = reg
                    if len(_REGION_CACHE) > 24:
                        for k in list(_REGION_CACHE)[:-12]:
                            try:
                                _REGION_CACHE[k].close()
                            except Exception:
                                pass
                            _REGION_CACHE.pop(k, None)
                ch = reg.chunk(ccx, ccz)
                if not ch:
                    continue
                key = (path, ccx, ccz)
                pix = _CHUNK_CACHE.get(key)
                if pix is None:
                    pix = ChunkPixels.render(ch)
                    _CHUNK_CACHE[key] = pix
                    if len(_CHUNK_CACHE) > 4096:
                        _CHUNK_CACHE.clear()
                if not pix:
                    continue
                names, ys = pix
                i = ((wz & 15) * 16) + (wx & 15)
                nm = names[i] if i < len(names) else "minecraft:air"
                y = ys[i] if i < len(ys) else 64
                col = block_color(nm)
                if col is None:
                    continue
                # 简单光照：相邻高度差做明暗
                shade = 1.0
                if (wx & 15) < 15:
                    j = (wz & 15) * 16 + ((wx & 15) + 1)
                    if j < len(ys):
                        dy = (ys[j] - y)
                        shade = 1.0 + max(-0.28, min(0.28, dy * 0.09))
                o = (py * tile + px_) * 3
                img[o] = max(0, min(255, int(col[0] * shade)))
                img[o + 1] = max(0, min(255, int(col[1] * shade)))
                img[o + 2] = max(0, min(255, int(col[2] * shade)))
                filled += 1
            except Exception:
                continue

    png = _to_png(bytes(img), tile, tile)
    import base64
    return {
        "ok": True,
        "png": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
        "bounds": {"x0": x0, "z0": z0, "x1": cx + half, "z1": cz + half, "span": span},
        "coverage": round(filled / float(tile * tile), 3),
        "dim": dim,
    }


_REGION_CACHE: Dict[str, Region] = {}
_CHUNK_CACHE: Dict[Tuple[str, int, int], Any] = {}


def _to_png(rgb: bytes, w: int, h: int) -> bytes:
    """手写 PNG 编码（避免依赖 Pillow）。"""
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += rgb[y * w * 3:(y + 1) * w * 3]

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def clear_cache() -> None:
    for r in _REGION_CACHE.values():
        r.close()
    _REGION_CACHE.clear()
    _CHUNK_CACHE.clear()


# ==================================================================
# 结构：优先读存档真实数据，其次按 seed 推测
# ==================================================================

STRUCTURE_LABEL = {
    "minecraft:stronghold": ("要塞", "#c04a9e"),
    "minecraft:village": ("村庄", "#4a9e5a"),
    "minecraft:village_plains": ("村庄", "#4a9e5a"),
    "minecraft:village_desert": ("村庄", "#4a9e5a"),
    "minecraft:village_savanna": ("村庄", "#4a9e5a"),
    "minecraft:village_snowy": ("村庄", "#4a9e5a"),
    "minecraft:village_taiga": ("村庄", "#4a9e5a"),
    "minecraft:desert_pyramid": ("沙漠神殿", "#d8a83c"),
    "minecraft:jungle_temple": ("丛林神庙", "#4a8a3a"),
    "minecraft:swamp_hut": ("沼泽小屋", "#6a5a3a"),
    "minecraft:witch_hut": ("沼泽小屋", "#6a5a3a"),
    "minecraft:igloo": ("雪屋", "#a8c8dc"),
    "minecraft:ocean_ruin": ("海底遗迹", "#3a7a8a"),
    "minecraft:shipwreck": ("沉船", "#8a6a3a"),
    "minecraft:buried_treasure": ("埋藏的宝藏", "#d8c83c"),
    "minecraft:mineshaft": ("废弃矿井", "#8a6a5a"),
    "minecraft:mineshaft_mesa": ("废弃矿井", "#8a6a5a"),
    "minecraft:fortress": ("下界要塞", "#c04a3a"),
    "minecraft:nether_fortress": ("下界要塞", "#c04a3a"),
    "minecraft:bastion_remnant": ("堡垒遗迹", "#7a4a3a"),
    "minecraft:end_city": ("末地城", "#9a6ad8"),
    "minecraft:monument": ("海底神殿", "#3a8a9a"),
    "minecraft:ocean_monument": ("海底神殿", "#3a8a9a"),
    "minecraft:mansion": ("林地府邸", "#5a3a2a"),
    "minecraft:woodland_mansion": ("林地府邸", "#5a3a2a"),
    "minecraft:ruined_portal": ("废弃传送门", "#7a4aa8"),
    "minecraft:pillager_outpost": ("掠夺者前哨站", "#8a7a5a"),
    "minecraft:ancient_city": ("远古城市", "#3a5a7a"),
    "minecraft:trail_ruins": ("古迹废墟", "#8a8a6a"),
    "minecraft:trial_chambers": ("试炼密室", "#6a7a8a"),
    "minecraft:nether_fossil": ("下界化石", "#6a5a4a"),
}


def scan_structures(world_dir: str, dim: str = "overworld",
                    limit_files: int = 400) -> List[Dict[str, Any]]:
    """扫描区域文件里已生成的结构（真实数据）。"""
    regs = region_files(world_dir, dim)
    out: List[Dict[str, Any]] = []
    seen = set()
    n = 0
    for (rx, rz), path in regs.items():
        if n >= limit_files:
            break
        n += 1
        try:
            reg = Region(path)
        except Exception:
            continue
        for ccz in range(32):
            for ccx in range(32):
                off, cnt = reg.entries[ccx + ccz * 32]
                if not off:
                    continue
                ch = reg.chunk(ccx, ccz)
                if not ch:
                    continue
                lv = _level_of(ch)
                st = lv.get("Structures") or lv.get("structures") or {}
                starts = st.get("Starts") or st.get("starts") or {}
                if not isinstance(starts, dict):
                    continue
                for sid, s in starts.items():
                    if not isinstance(s, dict):
                        continue
                    x = s.get("ChunkX", s.get("chunkx", s.get("chunkX")))
                    z = s.get("ChunkZ", s.get("chunkz", s.get("chunkZ")))
                    if x is None or z is None:
                        x = s.get("Pos", {}).get("X") if isinstance(s.get("Pos"), dict) else None
                        z = s.get("Pos", {}).get("Z") if isinstance(s.get("Pos"), dict) else None
                    if x is None or z is None:
                        continue
                    bx = int(x) * 16 + 8
                    bz = int(z) * 16 + 8
                    key = (sid, int(x), int(z))
                    if key in seen:
                        continue
                    seen.add(key)
                    label, color = STRUCTURE_LABEL.get(
                        sid, (sid.replace("minecraft:", ""), "#8a8a8a"))
                    out.append({"id": sid, "label": label, "color": color,
                                "x": bx, "z": bz, "real": True})
        reg.close()
    return out


# 1.18+ 结构推测参数（spacing, separation, salt）—— 仅供参考
STRUCTURE_GUESS = {
    "minecraft:stronghold": (None, None, None),   # 单独算法
    "minecraft:village": (32, 8, 10387312),
    "minecraft:desert_pyramid": (32, 8, 14357617),
    "minecraft:jungle_temple": (32, 8, 14357619),
    "minecraft:swamp_hut": (32, 8, 14357620),
    "minecraft:igloo": (32, 8, 14357618),
    "minecraft:ocean_ruin": (20, 8, 14357621),
    "minecraft:shipwreck": (24, 4, 165745295),
    "minecraft:buried_treasure": (1, 1, 0),
    "minecraft:mineshaft": (1, 1, 0),
    "minecraft:monument": (32, 5, 10387313),
    "minecraft:mansion": (80, 20, 10387319),
    "minecraft:pillager_outpost": (32, 8, 165745296),
    "minecraft:ruined_portal": (40, 15, 34222645),
    "minecraft:fortress": (27, 4, 30084232),
    "minecraft:bastion_remnant": (27, 4, 30084232),
    "minecraft:end_city": (20, 11, 10387313),
}


def guess_structures(seed: int, x0: int, z0: int, x1: int, z1: int) -> List[Dict[str, Any]]:
    """
    按 seed 推测结构位置（1.13+ 通用 salted-region 算法）。
    标注 real=False，UI 上会提示为推测。
    """
    out: List[Dict[str, Any]] = []
    if seed is None:
        return out
    for sid, (spacing, separation, salt) in STRUCTURE_GUESS.items():
        if not spacing or spacing <= 1:
            continue
        off = max(1, spacing - separation)
        rx0, rx1 = int(math.floor(x0 / 16.0 / spacing)) - 1, int(math.ceil(x1 / 16.0 / spacing)) + 1
        rz0, rz1 = int(math.floor(z0 / 16.0 / spacing)) - 1, int(math.ceil(z1 / 16.0 / spacing)) + 1
        if (rx1 - rx0) * (rz1 - rz0) > 40000:
            continue
        for rgx in range(rx0, rx1 + 1):
            for rgz in range(rz0, rz1 + 1):
                m = seed + rgx * 341873128712 + rgz * 132897987541 + salt
                r = JavaRandom(_i32(_i32(m) ^ 987234911))
                ox = r.next_int(off)
                oz = r.next_int(off)
                cx = rgx * spacing + ox
                cz = rgz * spacing + oz
                bx, bz = cx * 16 + 8, cz * 16 + 8
                if not (x0 <= bx <= x1 and z0 <= bz <= z1):
                    continue
                label, color = STRUCTURE_LABEL.get(sid, (sid, "#8a8a8a"))
                out.append({"id": sid, "label": label, "color": color,
                            "x": bx, "z": bz, "real": False})
    return out


def slime_chunks(seed: int, x0: int, z0: int, x1: int, z1: int) -> List[Dict[str, int]]:
    """返回范围内的史莱姆区块（区块坐标）。"""
    out: List[Dict[str, int]] = []
    if seed is None:
        return out
    cx0, cx1 = int(math.floor(x0 / 16.0)), int(math.ceil(x1 / 16.0))
    cz0, cz1 = int(math.floor(z0 / 16.0)), int(math.ceil(z1 / 16.0))
    if (cx1 - cx0) * (cz1 - cz0) > 60000:
        return out
    for cx in range(cx0, cx1 + 1):
        for cz in range(cz0, cz1 + 1):
            if is_slime_chunk(seed, cx, cz):
                out.append({"cx": cx, "cz": cz})
    return out


# ==================================================================
# 标点 / 规划
# ==================================================================

def marks_path(world_dir: str) -> str:
    d = os.path.join(core.ROOT_DIR, "maps")
    os.makedirs(d, exist_ok=True)
    key = "".join(c for c in os.path.basename(os.path.normpath(world_dir))
                  if c.isalnum() or c in "-_")[:48] or "world"
    return os.path.join(d, key + ".json")


def load_marks(world_dir: str) -> Dict[str, Any]:
    p = marks_path(world_dir)
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"marks": [], "routes": [], "areas": []}


def save_marks(world_dir: str, data: Dict[str, Any]) -> Dict[str, Any]:
    p = marks_path(world_dir)
    cur = load_marks(world_dir)
    for k in ("marks", "routes", "areas"):
        if k in data:
            cur[k] = data[k]
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=1)
    return cur
