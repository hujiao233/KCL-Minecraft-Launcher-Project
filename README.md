# KCL Minecraft Launcher Project（脆冰启动器）

> 由 kcstudio 出品的 Minecraft 离线启动器 —— 轻量、开箱即用。

## 📦 简介

本仓库托管 **[kcstudio] 脆冰启动器** 的发布版本。脆冰启动器是一款简洁易用的 Minecraft 启动器，无需复杂配置，下载即可运行。

## ⬇️ 下载

### 最新版本（推荐）：Releases 发行版

每个版本的安装包都发布在 [Releases](https://github.com/hujiao233/KCL-Minecraft-Launcher-Project/releases) 页面，包含：

| 文件 | 说明 |
| --- | --- |
| `[kc studio]脆冰启动器.exe` | 完整版（约 27 MB），内置便携 Python 运行环境，解压即用 |
| `压缩启动器.exe` | 精简版（约 9.6 MB），首次运行时自动下载运行环境并自我补全 |
| `启动器网页端.html` | 单文件网页版启动器（离线可用） |
| `脆冰启动器-源码-vX.Y.Z.zip` | 完整源码包 |

### 仓库直链（旧版本）

- [\[kcstudio\]脆冰启动器.exe](./%5Bkcstudio%5D脆冰启动器.exe) （约 9.9 MB）

也可以通过命令行下载：

```bash
curl -L -o "脆冰启动器.exe" "https://raw.githubusercontent.com/hujiao233/KCL-Minecraft-Launcher-Project/main/%5Bkcstudio%5D%E8%84%86%E5%86%B0%E5%90%AF%E5%8A%A8%E5%99%A8.exe"
```

## 🚀 使用方法

1. 从本仓库下载 `[kcstudio]脆冰启动器.exe`
2. 双击运行（如遇 SmartScreen 提示，选择「仍要运行」）
3. 在启动器内选择游戏版本并启动

> 💡 建议 Windows 10 / 11 系统，并已安装 Java 运行环境。

## 🛠 从源码运行

本机没有 Python 也能一键跑起来：

1. 双击 `启动源码.bat`（会自动调用 `prepare_python.ps1` 下载约 12 MB 的便携 Python 到 `runtime/` 并安装依赖）
2. 已装 Python 的用户可直接 `pip install -r requirements.txt` 后 `python main.py`

打包命令：

```bash
python build_exe.py          # 生成完整版 [kc studio]脆冰启动器.exe
python build_exe.py --slim   # 生成 压缩启动器.exe（自我增肥版）
python build_web.py          # 生成 启动器网页端.html
```

## ℹ️ 说明

- 本启动器仅供学习交流使用，请支持正版 Minecraft。
- 如遇问题，欢迎提交 [Issue](https://github.com/hujiao233/KCL-Minecraft-Launcher-Project/issues)。

## 📄 许可

启动器程序版权归 kcstudio 所有。

