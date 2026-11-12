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
            vals = list(struct.unpack_from(">%di" % n, self.b, self.i)) if n else []
            self.i += 4 * n          # unpack_from 不移动游标，必须手动推进
            return vals
        if t == 12:
            n = self._su(">i", 4)
            vals = list(struct.unpack_from(">%dq" % n, self.b, self.i)) if n else []
            self.i += 8 * n          # 同上：block_states.data / Heightmaps 都是 long[]
            return vals
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
    """
    Java 版史莱姆区块判定（精确）。
    常数与算法逐行对应 cubiomes finders.h 的 isSlimeChunk
    （旧实现的哈希常数有误，会导致约 15% 的区块判定错误，已修正）。
    """
    M64, M48 = (1 << 64) - 1, (1 << 48) - 1
    r = seed & M64
    r = (r + _i32(cx * 0x5ac0db)) & M64
    r = (r + _i32(cx * cx * 0x4c1906)) & M64
    r = (r + _i32(cz * 0x5f24f)) & M64
    r = (r + _i32(cz * cz) * 0x4307a7) & M64
    r ^= 0x3ad8025f
    r = (r ^ 0x5DEECE66D) & M48
    r = (r * 0x5DEECE66D + 0xB) & M48
    bits = r >> (48 - 31)                      # Java nextInt(10) 的 next(31)
    val = bits % 10
    if bits - val + 9 >= (1 << 31):            # 极罕见：拒绝采样
        return _slime_next_int10(r)
    return val == 0


def _slime_next_int10(state48: int) -> bool:  # pragma: no cover
    """Java nextInt(10) 拒绝采样路径（实际几乎不可能走到）。"""
    M48 = (1 << 48) - 1
    r = state48 & M48
    while True:
        r = (r * 0x5DEECE66D + 0xB) & M48
        bits = r >> 17
        val = bits % 10
        if bits - val + 9 < (1 << 31):
            return val == 0


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


def block_color(name: str):
    # 空气必须返回 None 让该像素留空；否则会被下面的「未知方块」哈希逻辑
    # 当成普通方块涂成同一种颜色，整张图变成纯色、覆盖率虚高到 100%
    if name in AIR:
        return None
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


# ==================================================================
# 区块地图缓存：把渲染过的区块颜色持久保存到磁盘
# 存档跑的时候不断加载新区块 -> 定时 learn 补进来 -> 地图越玩越完整
# 每个 region 一个定长 bin：1024 个槽，每槽 1 字节标记 + 16x16x3 字节 RGB
# ==================================================================
SLOT = 769
_STORE_FILES: Dict[Any, Any] = {}
_STORE_FLAGS: Dict[Any, Any] = {}
_STORE_META: Dict[str, Dict[str, Any]] = {}
_STORE_CHECKED: set = set()


def _cache_dir(world_dir: str, dim: str) -> str:
    import hashlib
    key = "".join(c for c in os.path.basename(os.path.normpath(world_dir))
                  if c.isalnum() or c in "-_")[:40] or "world"
    h = hashlib.md5(os.path.normpath(world_dir).encode("utf-8", "replace")).hexdigest()[:8]
    p = os.path.join(core.ROOT_DIR, "maps", "_cache", key + "-" + h, dim)
    os.makedirs(p, exist_ok=True)
    return p


def _bin_path(d: str, rx: int, rz: int) -> str:
    return os.path.join(d, "r.%d.%d.bin" % (rx, rz))


def _store_meta(d: str) -> Dict[str, Any]:
    m = _STORE_META.get(d)
    if m is None:
        try:
            with open(os.path.join(d, "meta.json"), "r", encoding="utf-8") as f:
                m = json.load(f)
        except Exception:
            m = {}
        if not isinstance(m, dict):
            m = {}
        _STORE_META[d] = m
    return m


def _store_save_meta(d: str) -> None:
    try:
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(_STORE_META.get(d, {}), f, ensure_ascii=False)
    except Exception:
        pass


def _bin_file(d: str, rx: int, rz: int):
    k = (d, rx, rz)
    f = _STORE_FILES.get(k)
    if f is None or f.closed:
        p = _bin_path(d, rx, rz)
        need = SLOT * 1024
        if not os.path.isfile(p) or os.path.getsize(p) != need:
            with open(p, "wb") as g:
                g.truncate(need)
        f = open(p, "r+b")
        _STORE_FILES[k] = f
        if len(_STORE_FILES) > 32:
            for kk, ff in list(_STORE_FILES.items())[:-16]:
                try:
                    ff.close()
                except Exception:
                    pass
                _STORE_FILES.pop(kk, None)
    return f


def _store_flags(d: str, rx: int, rz: int) -> bytearray:
    k = (d, rx, rz)
    fl = _STORE_FLAGS.get(k)
    if fl is None:
        f = _bin_file(d, rx, rz)
        f.seek(0)
        raw = f.read(SLOT * 1024)
        fl = bytearray(raw[i * SLOT] if i * SLOT < len(raw) else 0 for i in range(1024))
        _STORE_FLAGS[k] = fl
    return fl


def _store_ensure(d: str, path: str, rx: int, rz: int) -> None:
    """region 文件变了就重学它（游戏保存新区块时 size/mtime 会变）。"""
    try:
        st = os.stat(path)
        sig = [int(st.st_mtime), int(st.st_size)]
    except Exception:
        return
    meta = _store_meta(d)
    key = os.path.basename(path)
    if meta.get(key) == sig:
        return
    f = _STORE_FILES.pop((d, rx, rz), None)
    if f:
        try:
            f.close()
        except Exception:
            pass
    _STORE_FLAGS.pop((d, rx, rz), None)
    try:
        os.remove(_bin_path(d, rx, rz))
    except Exception:
        pass
    meta[key] = sig
    _store_save_meta(d)


def _store_has(d: str, rx: int, rz: int, ccx: int, ccz: int) -> bool:
    return _store_flags(d, rx, rz)[ccz * 32 + ccx] == 1


def _store_put(d: str, rx: int, rz: int, ccx: int, ccz: int, rgb: bytes) -> None:
    f = _bin_file(d, rx, rz)
    idx = ccz * 32 + ccx
    f.seek(idx * SLOT)
    f.write(b"\x01" + rgb)
    f.flush()
    _store_flags(d, rx, rz)[idx] = 1


def _store_get(d: str, rx: int, rz: int, ccx: int, ccz: int) -> Optional[bytes]:
    f = _bin_file(d, rx, rz)
    f.seek((ccz * 32 + ccx) * SLOT)
    b = f.read(SLOT)
    return b[1:] if len(b) == SLOT and b[0] == 1 else None


def _chunk_rgb(root: Dict[str, Any]) -> bytes:
    """把一个区块压成 16x16 的颜色表（光照也在这里固化，之后直接读缓存）。"""
    pix = ChunkPixels.render(root)
    out = bytearray(16 * 16 * 3)
    if not pix:
        return bytes(out)
    names, ys = pix
    for i, nm in enumerate(names):
        c = block_color(nm)
        if c is None:
            continue
        sh = 1.0
        if i % 16 < 15 and i + 1 < len(ys):
            dy = ys[i + 1] - ys[i]
            sh = 1.0 + max(-0.28, min(0.28, dy * 0.09))
        o = i * 3
        out[o] = max(0, min(255, int(c[0] * sh)))
        out[o + 1] = max(0, min(255, int(c[1] * sh)))
        out[o + 2] = max(0, min(255, int(c[2] * sh)))
    return bytes(out)


def _store_learn_chunk(d: str, path: str, rx: int, rz: int,
                       ccx: int, ccz: int, reg) -> None:
    ch = reg.chunk(ccx, ccz)
    _store_put(d, rx, rz, ccx, ccz, _chunk_rgb(ch) if ch else bytes(768))
    _REGION_CACHE[path] = reg


def store_stats(world_dir: str, dim: str,
                regs: Optional[Dict[Tuple[int, int], str]] = None) -> Dict[str, int]:
    """已记住 / 全部区块数。"""
    if regs is None:
        regs = region_files(world_dir, dim)
    d = _cache_dir(world_dir, dim)
    total = learned = 0
    for (rx, rz), path in regs.items():
        try:
            reg = _REGION_CACHE.get(path) or Region(path)
            _REGION_CACHE[path] = reg
            _store_ensure(d, path, rx, rz)
        except Exception:
            continue
        fl = _store_flags(d, rx, rz)
        for i in range(1024):
            off, cnt = reg.entries[i]
            if off and cnt:
                total += 1
                if fl[i] == 1:
                    learned += 1
    return {"learned": learned, "total": total}


def learn_chunks(world_dir: str, dim: str = "overworld",
                 budget: int = 240) -> Dict[str, Any]:
    """增量学习：每次最多解析 budget 个新区块，供前端定时调用（边玩边记）。"""
    regs = region_files(world_dir, dim)
    if not regs:
        return {"learned": 0, "total": 0, "done": True, "added": 0}
    d = _cache_dir(world_dir, dim)
    todo = []
    total = learned = 0
    for (rx, rz), path in sorted(regs.items()):
        try:
            reg = _REGION_CACHE.get(path) or Region(path)
            _REGION_CACHE[path] = reg
            _store_ensure(d, path, rx, rz)
        except Exception:
            continue
        fl = _store_flags(d, rx, rz)
        for i in range(1024):
            off, cnt = reg.entries[i]
            if not (off and cnt):
                continue
            total += 1
            if fl[i] == 1:
                learned += 1
            else:
                todo.append((path, rx, rz, i & 31, i >> 5, reg))
    added = 0
    for (path, rx, rz, ccx, ccz, reg) in todo:
        if added >= budget:
            break
        try:
            _store_learn_chunk(d, path, rx, rz, ccx, ccz, reg)
        except Exception:
            continue
        added += 1
        learned += 1
    return {"learned": learned, "total": total,
            "done": learned >= total, "added": added}


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
    cache = _cache_dir(world_dir, dim)
    for (rx, rz), path in regs.items():
        _store_ensure(cache, path, rx, rz)

    # 逐像素采样：区块颜色来自持久缓存，没有就现场解析并写回（下次直接读）
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
                if not _store_has(cache, rx, rz, ccx, ccz):
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
                    _store_learn_chunk(cache, path, rx, rz, ccx, ccz, reg)
                data = _store_get(cache, rx, rz, ccx, ccz)
                if data is None:
                    continue
                i = ((wz & 15) * 16) + (wx & 15)
                o = i * 3
                r, g, b = data[o], data[o + 1], data[o + 2]
                if r == 0 and g == 0 and b == 0:
                    continue
                po = (py * tile + px_) * 3
                img[po] = r; img[po + 1] = g; img[po + 2] = b
                filled += 1
            except Exception:
                continue

    png = _to_png(bytes(img), tile, tile)
    import base64
    stat = store_stats(world_dir, dim, regs)
    return {
        "ok": True,
        "png": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
        "bounds": {"x0": x0, "z0": z0, "x1": cx + half, "z1": cz + half, "span": span},
        "coverage": round(filled / float(tile * tile), 3),
        "dim": dim,
        "learned": stat["learned"], "total": stat["total"],
    }


_REGION_CACHE: Dict[str, Region] = {}
_CHUNK_CACHE: Dict[Tuple[str, int, int], Any] = {}
_CHUNK_MISS = object()


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
    for f in _STORE_FILES.values():
        try:
            f.close()
        except Exception:
            pass
    _STORE_FILES.clear()
    _STORE_FLAGS.clear()
    _STORE_META.clear()
    _STORE_CHECKED.clear()
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


# ==================================================================
# cubiomes：精确种子计算（群系 / 结构 / 史莱姆区块）
# 上游 https://github.com/Cubitect/cubiomes（MIT 协议，见 LICENSE-cubiomes.txt），
# 编译好的 cubiomes.dll 随启动器分发；缺 DLL 时自动退回旧的近似算法（仅能看个大概）。
# ==================================================================

_MC_ENUM = {   # biomes.h enum MCVersion（cubiomes master，MC_NEWEST = 28）
    "1.8.9": 11, "1.9.4": 12, "1.10.2": 13, "1.11.2": 14, "1.12.2": 15,
    "1.13.2": 16, "1.14.4": 17, "1.15.2": 18, "1.16.5": 20, "1.17.1": 21,
    "1.18.2": 22, "1.19.2": 23, "1.19.4": 24, "1.20.6": 25, "1.21.1": 26,
    "1.21.3": 27, "1.21.4+": 28,
}
CB_DEFAULT_VER = "1.21.4+"

_CB_DLL = None            # ctypes.CDLL 或 False（加载失败）
_CB_GEN: Dict[Tuple[int, int, int], Any] = {}   # (mc, dim, seed) -> Generator 缓冲

_CB_DIM = {"overworld": 0, "nether": -1, "end": 1}

# cubiomes finders.h enum StructureType 里对玩家有用的部分
_CB_ST_ID = {
    1: "minecraft:desert_pyramid", 2: "minecraft:jungle_temple",
    3: "minecraft:swamp_hut", 4: "minecraft:igloo", 5: "minecraft:village",
    6: "minecraft:ocean_ruin", 7: "minecraft:shipwreck", 8: "minecraft:monument",
    9: "minecraft:mansion", 10: "minecraft:pillager_outpost",
    11: "minecraft:ruined_portal", 12: "minecraft:ruined_portal",
    13: "minecraft:ancient_city", 14: "minecraft:buried_treasure",
    15: "minecraft:mineshaft", 23: "minecraft:trail_ruins",
    24: "minecraft:trial_chambers", 18: "minecraft:fortress",
    19: "minecraft:bastion_remnant", 20: "minecraft:end_city",
}
_CB_STRUCTS = {
    "overworld": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 23, 24],
    "nether": [18, 19, 12],
    "end": [20],
}

# 群系 id ->（中文名, RGB）。来源 biomes.h enum BiomeID（1.21）
CB_BIOME_COLORS = {
    0: ("海洋", 32, 88, 148), 1: ("平原", 120, 168, 76), 2: ("沙漠", 224, 198, 122),
    3: ("山地", 126, 124, 118), 4: ("森林", 54, 120, 56), 5: ("针叶林", 74, 112, 92),
    6: ("沼泽", 68, 98, 68), 7: ("河流", 44, 90, 150), 8: ("下界荒地", 104, 44, 38),
    9: ("末地", 196, 190, 160), 10: ("冻洋", 122, 142, 182), 11: ("冻河", 96, 120, 160),
    12: ("雪原", 226, 236, 242), 13: ("雪山", 198, 210, 220), 14: ("蘑菇岛", 138, 116, 190),
    15: ("蘑菇岛岸", 110, 96, 160), 16: ("沙滩", 216, 202, 148), 17: ("沙漠丘陵", 214, 186, 108),
    18: ("森林丘陵", 48, 108, 50), 19: ("针叶林丘陵", 66, 102, 86), 20: ("山地边缘", 120, 120, 112),
    21: ("丛林", 42, 130, 52), 22: ("丛林丘陵", 38, 118, 48), 23: ("丛林边缘", 60, 140, 60),
    24: ("深海", 18, 52, 104), 25: ("石岸", 150, 148, 140), 26: ("雪滩", 208, 218, 224),
    27: ("白桦林", 140, 180, 100), 28: ("白桦林丘陵", 132, 172, 94), 29: ("黑森林", 40, 84, 36),
    30: ("积雪针叶林", 150, 180, 180), 31: ("积雪针叶林丘陵", 140, 170, 172),
    32: ("原始云杉针叶林", 62, 96, 78), 33: ("原始云杉针叶林丘陵", 58, 90, 74),
    34: ("风袭丘陵", 110, 112, 108), 35: ("热带草原", 182, 168, 82), 36: ("热带高原", 166, 152, 80),
    37: ("恶地", 178, 96, 60), 38: ("疏林恶地高原", 166, 88, 54), 39: ("恶地高原", 172, 92, 58),
    40: ("末地小型岛屿", 180, 176, 150), 41: ("末地中型岛屿", 188, 184, 158),
    42: ("末地高岛", 198, 194, 168), 43: ("末地荒岛", 170, 166, 140),
    44: ("暖洋", 44, 110, 168), 45: ("温水洋", 38, 100, 158), 46: ("冷水洋", 26, 74, 124),
    47: ("暖深海", 36, 92, 148), 48: ("温水深海", 30, 82, 134), 49: ("冷水深海", 18, 52, 104),
    50: ("冻深海", 14, 44, 92), 127: ("虚空", 24, 24, 32),
    129: ("向日葵平原", 130, 172, 80), 130: ("沙漠湖泊", 222, 192, 110),
    131: ("砂砾山地", 138, 136, 130), 132: ("繁花森林", 70, 140, 80),
    133: ("针叶山地", 80, 116, 96), 134: ("沼泽丘陵", 72, 100, 70),
    135: ("冰刺之地", 232, 240, 246), 136: ("丛林变种", 38, 126, 50),
    137: ("丛林边缘变种", 56, 136, 56), 138: ("高大白桦林", 146, 186, 106),
    139: ("高大白桦丘陵", 140, 178, 100), 140: ("黑森林丘陵", 36, 78, 32),
    141: ("积雪针叶山地", 156, 186, 186), 142: ("原始云杉针叶林变种", 58, 92, 76),
    143: ("原始云杉针叶林丘陵变种", 56, 88, 72), 144: ("风袭丘陵砾石变种", 120, 122, 118),
    145: ("破碎热带草原", 186, 164, 76), 146: ("破碎热带高原", 172, 150, 72),
    147: ("风蚀恶地", 184, 100, 64), 148: ("疏林恶地高原变种", 162, 84, 52),
    149: ("恶地高原变种", 168, 88, 56), 168: ("竹林", 56, 138, 56), 169: ("竹林丘陵", 52, 132, 52),
    170: ("灵魂沙峡谷", 94, 72, 88), 171: ("绯红森林", 150, 40, 44),
    172: ("诡异森林", 42, 140, 130), 173: ("玄武岩三角洲", 70, 60, 66),
    174: ("溶洞", 170, 140, 110), 175: ("繁茂洞穴", 90, 150, 90),
    177: ("樱花树林", 222, 178, 190), 183: ("深暗之域", 20, 32, 48),
    184: ("红树林沼泽", 90, 120, 60), 185: ("樱花树林", 232, 170, 186),
    186: ("苍白之园", 158, 168, 156),
}


def _load_cubiomes():
    """加载随包分发的 cubiomes.dll；失败返回 None 且不再重试。"""
    global _CB_DLL
    if _CB_DLL is not None:
        return _CB_DLL if _CB_DLL is not False else None
    here = os.path.dirname(os.path.abspath(__file__))
    path = next((p for p in (os.path.join(here, "cubiomes.dll"),
                             os.path.join(here, "bin", "cubiomes.dll"))
                 if os.path.isfile(p)), None)
    if not path:
        _CB_DLL = False
        return None
    try:
        import ctypes
        dll = ctypes.CDLL(path)
        I, V, L = ctypes.c_int, ctypes.c_void_p, ctypes.c_longlong
        dll.cb_generator_size.restype = I
        dll.cb_setup.argtypes = [V, I]
        dll.cb_apply_seed.argtypes = [V, I, L]
        dll.cb_biome_at.restype = I
        dll.cb_biome_at.argtypes = [V, I, I, I, I]
        dll.cb_structures_area.restype = I
        dll.cb_structures_area.argtypes = [I, I, L, V, I, I, I, I,
                                           ctypes.POINTER(I), ctypes.POINTER(I),
                                           I, ctypes.POINTER(I)]
        dll.cb_slime_chunk.restype = I
        dll.cb_slime_chunk.argtypes = [L, I, I]
        dll.cb_stronghold_size.restype = I
        dll.cb_stronghold_init.argtypes = [V, I, L, ctypes.POINTER(I), ctypes.POINTER(I)]
        dll.cb_stronghold_next.restype = I
        dll.cb_stronghold_next.argtypes = [V, V, ctypes.POINTER(I), ctypes.POINTER(I)]
        dll.cb_spawn_pos.argtypes = [V, ctypes.POINTER(I), ctypes.POINTER(I)]
        _CB_DLL = dll
        return dll
    except Exception:
        _CB_DLL = False
        return None


def cb_accurate() -> bool:
    """cubiomes 是否可用（决定 SeedMap 是「精确」还是「近似推测」）。"""
    return _load_cubiomes() is not None


def map_versions() -> List[Dict[str, Any]]:
    return [{"id": k, "label": k} for k in _MC_ENUM]


def map_mc(ver: Any) -> Optional[int]:
    return _MC_ENUM.get(str(ver or "").strip())


def _cb_generator(mc: int, dim: int, seed: int):
    import ctypes
    dll = _load_cubiomes()
    key = (mc, dim, seed)
    g = _CB_GEN.get(key)
    if g is None:
        g = ctypes.create_string_buffer(dll.cb_generator_size())
        dll.cb_setup(g, mc)
        dll.cb_apply_seed(g, dim, seed)
        if len(_CB_GEN) > 24:
            _CB_GEN.clear()
        _CB_GEN[key] = g
    return g


_CB_STRONGHOLD_CACHE: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}


def _cb_strongholds(mc: int, seed: int) -> List[Tuple[int, int]]:
    """128 个要塞的精确位置（按 seed+版本缓存，扫描视野时不重算）。"""
    got = _CB_STRONGHOLD_CACHE.get((mc, seed))
    if got is not None:
        return got
    import ctypes as ct
    dll = _load_cubiomes()
    g = _cb_generator(mc, 0, seed)
    sh = ct.create_string_buffer(dll.cb_stronghold_size())
    hx, hz = ct.c_int(), ct.c_int()
    dll.cb_stronghold_init(sh, mc, seed, ct.byref(hx), ct.byref(hz))
    out: List[Tuple[int, int]] = []
    for _ in range(128):
        try:
            if dll.cb_stronghold_next(sh, g, ct.byref(hx), ct.byref(hz)) <= 0:
                break
        except Exception:
            break
        out.append((hx.value, hz.value))
    if len(_CB_STRONGHOLD_CACHE) > 8:
        _CB_STRONGHOLD_CACHE.clear()
    _CB_STRONGHOLD_CACHE[(mc, seed)] = out
    return out


def _cb_structures(seed: int, x0: int, z0: int, x1: int, z1: int,
                   dim: str, mc: int) -> List[Dict[str, Any]]:
    """cubiomes 精确结构位置（含群系校验），加出生点与要塞。"""
    import ctypes as ct
    dll = _load_cubiomes()
    g = _cb_generator(mc, _CB_DIM[dim], seed)
    xs = (ct.c_int * 4096)()
    zs = (ct.c_int * 4096)()
    dimref = ct.c_int()
    out: List[Dict[str, Any]] = []
    for st in _CB_STRUCTS.get(dim, []):
        n = dll.cb_structures_area(st, mc, seed, g, int(x0), int(z0), int(x1), int(z1),
                                   xs, zs, 4096, ct.byref(dimref))
        if n <= 0:
            continue
        sid = _CB_ST_ID.get(st, "minecraft:structure_%d" % st)
        label, color = STRUCTURE_LABEL.get(sid, (sid.replace("minecraft:", ""), "#8a8a8a"))
        for i in range(n):
            out.append({"id": sid, "label": label, "color": color,
                        "x": xs[i], "z": zs[i], "real": True})
    if dim == "overworld":
        sx, sz = ct.c_int(), ct.c_int()
        dll.cb_spawn_pos(g, ct.byref(sx), ct.byref(sz))
        if x0 <= sx.value <= x1 and z0 <= sz.value <= z1:
            out.append({"id": "minecraft:spawn", "label": "出生点", "color": "#7ad8ff",
                        "x": sx.value, "z": sz.value, "real": True})
        # 要塞：环形共 128 个，逐个迭代（cubiomes 官方算法），按 seed+版本缓存
        for hx, hz in _cb_strongholds(mc, seed):
            out.append({"id": "minecraft:stronghold", "label": "要塞", "color": "#c04a9e",
                        "x": hx, "z": hz, "real": True})
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


# 各结构所属维度：主世界绝不能出现末地城 / 下界要塞 / 堡垒遗迹
NETHER_ONLY = {
    "minecraft:fortress", "minecraft:nether_fortress",
    "minecraft:bastion_remnant", "minecraft:nether_fossil",
}
END_ONLY = {"minecraft:end_city"}
BOTH_PORTAL = {"minecraft:ruined_portal"}


def structures_for_dim(sid: str, dim: str) -> bool:
    """这个结构 id 是否属于该维度。"""
    if dim == "nether":
        return sid in NETHER_ONLY or sid in BOTH_PORTAL
    if dim == "end":
        return sid in END_ONLY
    return sid not in NETHER_ONLY and sid not in END_ONLY


def guess_structures(seed: int, x0: int, z0: int, x1: int, z1: int,
                     dim: str = "overworld", mc: Any = None) -> List[Dict[str, Any]]:
    """
    按 seed 推测结构位置。mc 给出 MC 版本且 cubiomes 可用时走精确算法
    （带群系校验：沙漠神殿只落在沙漠、海底神殿只在深海……）；
    否则退回旧的 salted-region 近似（无群系校验，位置仅供参考）。
    """
    if seed is None:
        return []
    if _load_cubiomes() and map_mc(mc):
        try:
            return _cb_structures(seed, x0, z0, x1, z1, dim, map_mc(mc))
        except Exception:
            pass
    out: List[Dict[str, Any]] = []
    for sid, (spacing, separation, salt) in STRUCTURE_GUESS.items():
        if not spacing or spacing <= 1:
            continue
        if not structures_for_dim(sid, dim):
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
    """返回范围内的史莱姆区块（区块坐标）。cubiomes 可用时用精确实现。"""
    out: List[Dict[str, int]] = []
    if seed is None:
        return out
    cx0, cx1 = int(math.floor(x0 / 16.0)), int(math.ceil(x1 / 16.0))
    cz0, cz1 = int(math.floor(z0 / 16.0)), int(math.ceil(z1 / 16.0))
    if (cx1 - cx0) * (cz1 - cz0) > 60000:
        return out
    dll = _load_cubiomes()
    fn = dll.cb_slime_chunk if dll else None
    for cx in range(cx0, cx1 + 1):
        for cz in range(cz0, cz1 + 1):
            if (fn(seed, cx, cz) if fn else is_slime_chunk(seed, cx, cz)):
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


# ==================================================================
# SeedMap：由「用户选择的种子」推测地形 / 群系底图
# ==================================================================

def seed_to_long(raw: Any) -> Optional[int]:
    """数字种子直接用；文字种子按 Java String.hashCode 转成 32 位有符号数。"""
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        return int(s)
    except Exception:
        pass
    h = 0
    for ch in s:
        h = (31 * h + ord(ch)) & 0xFFFFFFFF
    return h - 0x100000000 if h >= 0x80000000 else h


BIOME_PALETTE = {
    "deep_ocean": ("深海", (18, 52, 104)),
    "ocean": ("海洋", (32, 88, 148)),
    "beach": ("沙滩", (216, 202, 148)),
    "plains": ("平原", (120, 168, 76)),
    "forest": ("森林", (54, 120, 56)),
    "birch_forest": ("白桦林", (140, 180, 100)),
    "jungle": ("丛林", (42, 130, 52)),
    "swamp": ("沼泽", (68, 98, 68)),
    "desert": ("沙漠", (224, 198, 122)),
    "savanna": ("热带草原", (182, 168, 82)),
    "badlands": ("恶地", (178, 96, 60)),
    "taiga": ("针叶林", (74, 112, 92)),
    "snowy": ("雪原", (226, 236, 242)),
    "mountains": ("山地", (126, 124, 118)),
    "peak": ("雪峰", (200, 208, 214)),
    "nether_wastes": ("下界荒地", (104, 44, 38)),
    "nether_lava": ("岩浆海", (168, 72, 32)),
    "end": ("末地", (196, 190, 160)),
    "end_void": ("末地虚空", (152, 148, 132)),
}


def _h2(seed: int, x: int, z: int) -> float:
    h = (seed ^ (x * 374761393) ^ (z * 668265263)) & 0xFFFFFFFF
    h = ((h ^ (h >> 13)) * 1274126177) & 0xFFFFFFFF
    return ((h ^ (h >> 16)) & 0xFFFF) / 65535.0


def _vnoise(seed: int, x: float, z: float, scale: float, salt: int = 0) -> float:
    """平滑值噪声（确定性，同一 seed 永远得到同一张图）。"""
    fx, fz = x / scale, z / scale
    ix, iz = math.floor(fx), math.floor(fz)
    tx, tz = fx - ix, fz - iz
    tx = tx * tx * (3 - 2 * tx)
    tz = tz * tz * (3 - 2 * tz)
    s = seed + salt
    a, b = _h2(s, ix, iz), _h2(s, ix + 1, iz)
    c, d = _h2(s, ix, iz + 1), _h2(s, ix + 1, iz + 1)
    return (a * (1 - tx) + b * tx) * (1 - tz) + (c * (1 - tx) + d * tx) * tz


def biome_at(seed: int, wx: float, wz: float, dim: str = "overworld") -> str:
    """按气候噪声推测某点的群系（推测，不保证与 MC 生成完全一致）。"""
    if dim == "nether":
        return "nether_lava" if _vnoise(seed, wx, wz, 220, 71) < 0.28 else "nether_wastes"
    if dim == "end":
        return "end_void" if _vnoise(seed, wx, wz, 380, 97) < 0.35 else "end"
    cont = 0.62 * _vnoise(seed, wx, wz, 700, 11) + 0.38 * _vnoise(seed, wx, wz, 260, 23)
    temp = _vnoise(seed, wx, wz, 600, 37)
    humid = _vnoise(seed, wx, wz, 450, 53)
    height = 0.65 * _vnoise(seed, wx, wz, 260, 67) + 0.35 * _vnoise(seed, wx, wz, 90, 83)
    if cont < 0.36:
        return "deep_ocean"
    if cont < 0.44:
        return "ocean"
    if cont < 0.475:
        return "beach"
    if height > 0.75:
        return "peak" if temp < 0.35 else "mountains"
    if temp > 0.70 and humid < 0.38:
        return "badlands" if height > 0.62 else "desert"
    if temp > 0.66 and humid > 0.66:
        return "jungle"
    if 0.42 < temp < 0.72 and humid > 0.74:
        return "swamp"
    if temp < 0.28:
        return "snowy" if humid < 0.6 else "taiga"
    if temp > 0.66 and humid < 0.55:
        return "savanna"
    if humid > 0.60:
        return "birch_forest" if temp > 0.55 else "forest"
    return "plains"


def _render_seedmap_cb(seed: int, dim: str, center: Tuple[float, float],
                       span: int, tile: int, mc_num: int, mc_label: Any) -> Dict[str, Any]:
    """cubiomes 精确群系底图（每 4×4 方格取一次 MC 真实群系 id）。"""
    import base64
    dll = _load_cubiomes()
    cx, cz = center
    x0, z0 = cx - span / 2.0, cz - span / 2.0
    px_per_block = tile / float(span)
    img = bytearray(tile * tile * 3)
    counts: Dict[int, int] = {}
    step = max(1, tile // 160)
    cells = 0
    g = _cb_generator(mc_num, _CB_DIM[dim], seed)
    for by in range(0, tile, step):
        wz = z0 + by / px_per_block
        for bx in range(0, tile, step):
            wx = x0 + bx / px_per_block
            bid = dll.cb_biome_at(g, 4, int(wx), 64, int(wz))
            counts[bid] = counts.get(bid, 0) + 1
            cells += 1
            info = CB_BIOME_COLORS.get(bid)
            if info:
                r, gg, b = info[1], info[2], info[3]
            else:                     # 未知群系：给个稳定的可辨识色
                h = (bid * 2654435761) & 0xFFFF
                r, gg, b = 96 + ((h >> 8) & 0x3F), 96 + ((h >> 4) & 0x3F), 96 + (h & 0x3F)
            sh = 0.88 + 0.24 * _vnoise(seed, wx, wz, 48, 131)
            pix = bytes((max(0, min(255, int(r * sh))),
                         max(0, min(255, int(gg * sh))),
                         max(0, min(255, int(b * sh)))))
            for yy in range(by, min(by + step, tile)):
                base = yy * tile * 3
                for xx in range(bx, min(bx + step, tile)):
                    o = base + xx * 3
                    img[o] = pix[0]; img[o + 1] = pix[1]; img[o + 2] = pix[2]
    legend = []
    for bid, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        info = CB_BIOME_COLORS.get(bid)
        if info:
            name, col = info[0], "#%02x%02x%02x" % (info[1], info[2], info[3])
        else:
            name, col = "群系 %d" % bid, "#606060"
        legend.append({"id": str(bid), "name": name, "color": col,
                       "ratio": round(n / float(cells or 1), 4)})
    png = _to_png(bytes(img), tile, tile)
    return {
        "ok": True,
        "png": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
        "bounds": {"x0": x0, "z0": z0, "x1": cx + span / 2.0,
                   "z1": cz + span / 2.0, "span": span},
        "legend": legend, "dim": dim, "seed": seed, "coverage": 1.0,
        "accurate": True, "ver": str(mc_label),
    }


def render_seedmap(seed: int, dim: str = "overworld",
                   center: Tuple[float, float] = (0, 0),
                   span: int = 2048, tile: int = 320,
                   mc: Any = None) -> Dict[str, Any]:
    """
    画一张种子地形图（群系色块）。
    cubiomes 可用且给出版本时按 MC 真实生成算法逐群系精确计算；
    否则退回旧的自造噪声近似（只能看个大概，边界与真实游戏对不上）。
    """
    import base64
    if _load_cubiomes() and map_mc(mc):
        try:
            return _render_seedmap_cb(seed, dim, center, span, tile, map_mc(mc), mc)
        except Exception:
            pass
    cx, cz = center
    x0, z0 = cx - span / 2.0, cz - span / 2.0
    px_per_block = tile / float(span)
    img = bytearray(tile * tile * 3)
    counts: Dict[str, int] = {}
    step = max(1, tile // 160)          # 块采样，群系边界本来就是渐变的
    cells = 0
    for by in range(0, tile, step):
        wz = z0 + by / px_per_block
        for bx in range(0, tile, step):
            wx = x0 + bx / px_per_block
            key = biome_at(seed, wx, wz, dim)
            counts[key] = counts.get(key, 0) + 1
            cells += 1
            r, g, b = BIOME_PALETTE[key][1]
            sh = 0.88 + 0.24 * _vnoise(seed, wx, wz, 48, 131)
            pix = bytes((max(0, min(255, int(r * sh))),
                         max(0, min(255, int(g * sh))),
                         max(0, min(255, int(b * sh)))))
            for yy in range(by, min(by + step, tile)):
                base = yy * tile * 3
                for xx in range(bx, min(bx + step, tile)):
                    o = base + xx * 3
                    img[o] = pix[0]; img[o + 1] = pix[1]; img[o + 2] = pix[2]
    legend = []
    for k, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        cn, col = BIOME_PALETTE[k]
        legend.append({"id": k, "name": cn,
                       "color": "#%02x%02x%02x" % col,
                       "ratio": round(n / float(cells or 1), 4)})
    png = _to_png(bytes(img), tile, tile)
    return {
        "ok": True,
        "png": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
        "bounds": {"x0": x0, "z0": z0, "x1": cx + span / 2.0,
                   "z1": cz + span / 2.0, "span": span},
        "legend": legend, "dim": dim, "seed": seed, "coverage": 1.0,
        "accurate": False,
    }
