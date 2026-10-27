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

## 📝 更新日志

### 2026-10-27　仓库整理（启动器本身没有任何改动）

这次没动软件的功能，纯粹是把「放代码的仓库」收拾了一遍。跟你有关系的就这几件事：

- **安装包改到 Releases 页面下载了。** 以前 exe 和源码压缩包是直接堆在仓库里的，每出一个版本就多塞 10 MB 进去，仓库越来越臃肿，也不容易看清每次到底改了哪几行代码。现在它们统一挂到右侧的 Releases 页面，仓库里只保留干净的源码——这一下瘦了约 11 MB。
- **删掉了一个重复的界面文件。** 根目录的 `index.html` 和 `ui/index.html` 内容一字不差，各占 337 KB。程序实际读的是 `ui/` 里那份，根目录的属于多余的副本。
- **开源许可终于写清楚了。** 之前只写了一句「版权归 kcstudio 所有」，按默认规则这意味着别人既不能用也不能改。现在补上了 GPL-3.0：可以免费用、随便改、随便分发，但只要把改过的版本发出去，就得把改的部分同样开源出来。
- **以后有自动检查了。** 每次提交代码，GitHub 都会自动把全部源码编译一遍确认没写错语法，顺手再打包出一个精简版 exe 供下载（保留 7 天）。避免手滑提交一份跑不起来的版本。

> 如果你是从仓库里直接右键下载 exe 的老用户，请改去 [Releases 页面](https://github.com/hujiao233/KCL-Minecraft-Launcher-Project/releases) 下载，旧链接已经失效。

### 2026-10-27　服务端代码拆分 + 补上自动化测试（同样不影响使用）

接上一次整理，这次动的是「代码的写法」，不是功能：

- **网站服务端不再是一个 2165 行的超大文件。** 以前 `update_server.py` 一个文件里同时装着数据库、口令、版本发布、社区、联机、页面和网络请求，找个地方要在几千行里翻。现在按职责拆成 8 个小文件（`upd_config` 配置、`upd_db` 数据库、`upd_auth` 口令登录、`upd_releases` 版本发布、`upd_posts` 社区、`upd_servers` 联机、`upd_templates` 页面、`upd_handler` 网络请求），看名字就知道去哪儿找。
- **拆之前和拆之后逐条对过行为。** 不是拆完就发：新旧两份服务端同时跑起来，依次请求 8 个常用接口（健康检查、更新清单、网站首页、管理后台、版本列表、帖子列表、服务器搜索等），返回的状态码和内容长度完全一致，确认功能没有任何变化。
- **补了 61 个自动化测试。** 重点覆盖那些「用起来不容易发现、一旦坏了很麻烦」的地方：文件路径里混进 `../` 会不会写到数据目录外面、重复点赞会不会把点赞数算成负数、连发两个版本时会不会同时存在两个「最新版」、别人提的建议会不会被不相干的人看到、服务器联系方式是不是只有加入后才可见、登录状态过期会不会自动清理。
- **以后每次提交都会自动跑一遍测试。** 改坏了会在提交旁边直接标红，不用等有人用的时候才发现。

## ℹ️ 说明

- 本启动器仅供学习交流使用，请支持正版 Minecraft。
- 如遇问题，欢迎提交 [Issue](https://github.com/hujiao233/KCL-Minecraft-Launcher-Project/issues)。

## 📄 许可

版权所有 © kcstudio。

源码采用 [GNU General Public License v3.0](./LICENSE) 授权分发。基于本项目二次开发并对外发布时，须同样以 GPL-3.0 开源你所修改的全部代码。

Minecraft 是 Mojang Studios 的商标，与本许可证无关。

