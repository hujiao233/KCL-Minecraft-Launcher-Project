# KCL Minecraft Launcher Project（脆冰启动器）

> 由 kcstudio 出品的 Minecraft 离线启动器 —— 轻量、开箱即用。

## 📦 简介

本仓库托管 **[kcstudio] 脆冰启动器** 的发布版本。脆冰启动器是一款简洁易用的 Minecraft 启动器，无需复杂配置，下载即可运行。

## ⬇️ 下载

### 最新版本（推荐）：Releases 发行版

每个版本的安装包都发布在 [Releases](https://github.com/hujiao233/KCL-Minecraft-Launcher-Project/releases) 页面，包含：

| 文件 | 说明 |
| --- | --- |
| `[kc studio]脆冰启动器.exe` | 完整版（约 42 MB），内置便携 Python 运行环境，解压即用 |
| `压缩启动器.exe` | 精简版（约 10 MB），首次运行时自动下载运行环境并自我补全 |
| `启动器网页端.html` | 单文件网页版启动器（离线可用） |
| `脆冰启动器-源码-vX.Y.Z.zip` | 完整源码包 |

### 命令行下载

安装包只在 Releases 提供，不再由 Git 版本管理。可以用 `gh` 一次拉取最新版的全部附件：

```bash
gh release download --repo hujiao233/KCL-Minecraft-Launcher-Project
```

也可以到 [Releases 最新版](https://github.com/hujiao233/KCL-Minecraft-Launcher-Project/releases/latest) 页面手动挑选需要的文件。

## 🚀 使用方法

1. 从 [Releases 页面](https://github.com/hujiao233/KCL-Minecraft-Launcher-Project/releases) 下载 `[kcstudio]脆冰启动器.exe`
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

按版本号记录，一个版本号下面就是这个版本的全部改动。软件里的「更新日志」也能随时翻看。

### v1.2.2（2026-10-27）　更新后告诉你这次改了什么

**用起来能感觉到的**

- **修好了精简版第一次启动就装不上依赖库。** 便携版 Python 不带 `distutils`，给 pip 打底的脚本一跑就崩，结果依赖库一个都装不上，第一次打开直接报「No module named 'requests'」就退出了。现在改成直接下载 pip 的 wheel 解压（不依赖 `distutils`），装不上会换镜像重试并把原因写进日志，实在装不上会明确提示检查网络，而不是静默启动后崩溃。完整版也因此第一次真正内置了运行环境（以前打出来的其实还是精简版）。
- **升级之后会告诉你这次改了什么。** 更新完第一次打开会弹出一个窗口，列出这一版的变化：新增了什么、修好了什么问题。看完点「知道了」就关掉，之后再打开不会再弹，不用再去翻仓库查这次到底更新了什么。
- **更新说明按版本号管理。** 每个版本的改动都归在自己的版本号下面，不会出现版本号和说明对不上的情况。

**幕后整理**（不影响正常使用）

- **安装包改到 Releases 页面下载了。** 以前 exe 和源码压缩包直接堆在仓库里，每出一个版本就多塞 10 MB 进去，仓库越来越臃肿，也不容易看清每次改了哪几行代码。现在统一挂到 Releases 页面，仓库里只留干净的源码，一下瘦了约 11 MB。
- **删掉了一个重复的界面文件。** 根目录的 `index.html` 和 `ui/index.html` 内容一字不差，各占 337 KB，程序实际读的是 `ui/` 里那份。
- **服务端代码拆开了。** 以前 `update_server.py` 一个文件装着数据库、口令、版本发布、社区、联机、页面和网络请求。现在按职责拆成几个小模块，并且把新旧两份同时跑起来比对过 8 个接口，返回完全一致。
- **补了自动化测试。** 覆盖那些「用起来不容易发现、一旦坏了很麻烦」的地方：文件路径混进 `../` 会不会写到数据目录外面、重复点赞会不会把点赞数算成负数、连发两个版本会不会同时存在两个「最新版」、别人提的建议会不会被不相干的人看到、服务器联系方式是不是只有加入后才可见。
- **以后每次提交都会自动检查。** GitHub 会自动编译全部源码确认没写错语法，再跑一遍上面这些测试，顺便打包一个精简版 exe 备用。改坏了会在提交旁边直接标红。
- **开源许可写清楚了。** 之前只写了一句话「版权归 kcstudio 所有」，按默认规则这意味着别人既不能用也不能改。现在补上了 GPL-3.0：可以免费用、随便改、随便分发，只要把改过的版本发出去时同样开源。

> 老用户注意：以前从仓库里直接右键下载 exe 的方式已经失效，请到 [Releases 页面](https://github.com/hujiao233/KCL-Minecraft-Launcher-Project/releases) 下载。

### v1.2.1（2026-10-26）　修复「启动不带模组」

从 PCL / HMCL 迁过来的版本启动时读不到自己的模组，这一版改成自动识别这类版本的目录结构；模组目录里有东西却没有加载器时会直接提示「这些模组不会生效」。

### v1.2.0（2026-10-25）　实时内存优化

内存改成每次启动时按机器此刻真正可用的内存重新计算，不再把系统挤爆；新增「一键内存优化」与实时状态显示。

### v1.1.x（2026-10-25）

启动链路重构（借鉴 PCL）：Java 选择改为按版本区间匹配、挑不到就自动下载。

### v1.0.x（2026-10-24）

首个版本：基本的版本安装、账户登录与启动流程。

> 每个版本的逐条说明（含 v1.0、v1.0.2、v1.1.0、v1.1.1）保存在源码 [`server.py` 的 CHANGELOG](./server.py) 里，软件内的「更新日志」窗口会原样显示这些内容。

## ℹ️ 说明

- 本启动器仅供学习交流使用，请支持正版 Minecraft。
- 如遇问题，欢迎提交 [Issue](https://github.com/hujiao233/KCL-Minecraft-Launcher-Project/issues)。

## 📄 许可

版权所有 © kcstudio。

源码采用 [GNU General Public License v3.0](./LICENSE) 授权分发。基于本项目二次开发并对外发布时，须同样以 GPL-3.0 开源你所修改的全部代码。

Minecraft 是 Mojang Studios 的商标，与本许可证无关。

