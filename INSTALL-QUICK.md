# video-ai-cut 一键安装（复制即装）

> 一个 Git 链接，WorkBuddy 和 Codex 都能用。
> 本技能遵循通用 **SKILL.md 标准**，两个平台共用同一份文件，只是安装目录不同。

**仓库链接：`https://github.com/wufrancis16-sketch/video-ai-cut.git`**

---

## 🪟 Windows 用户（最简单）

**方式 A：GitHub 能访问时**，在命令行粘贴这一条：

```bat
git clone --depth 1 https://github.com/wufrancis16-sketch/video-ai-cut.git "%USERPROFILE%\.workbuddy\skills\video-ai-cut" && cd "%USERPROFILE%\.workbuddy\skills\video-ai-cut" && install.bat
```

**方式 B：GitHub 慢/打不开时**（用镜像），粘贴：

```bat
git clone --depth 1 https://ghproxy.com/https://github.com/wufrancis16-sketch/video-ai-cut.git "%USERPROFILE%\.workbuddy\skills\video-ai-cut" && cd "%USERPROFILE%\.workbuddy\skills\video-ai-cut" && install.bat
```

`install.bat` 会自动完成：
1. 安装到 WorkBuddy 技能目录 `%USERPROFILE%\.workbuddy\skills\video-ai-cut`
2. 安装到 Codex 技能目录 `%USERPROFILE%\.codex\skills\video-ai-cut`
3. 安装 Python 依赖（自动 `pip install -r requirements.txt`）
4. 检测/自动装 FFmpeg（装不上会提示手动安装）
5. 运行 `verify_skill.py` 自检（9 项，全 PASS 即可用）

> 前提：装好 [Git](https://git-scm.com/download/win) 和 [Python 3.10+](https://www.python.org/downloads/)（安装时勾选 Add to PATH）。

---

## 🐧 macOS / Linux 用户

```bash
git clone --depth 1 https://github.com/wufrancis16-sketch/video-ai-cut.git ~/.workbuddy/skills/video-ai-cut && cd ~/.workbuddy/skills/video-ai-cut && bash install.sh
```

---

## 📦 手动安装（不想用脚本）

```bash
# WorkBuddy
git clone https://github.com/wufrancis16-sketch/video-ai-cut.git ~/.workbuddy/skills/video-ai-cut
# Codex（可选，二选一或都要）
git clone https://github.com/wufrancis16-sketch/video-ai-cut.git ~/.codex/skills/video-ai-cut
# 依赖
pip install -r ~/.workbuddy/skills/video-ai-cut/requirements.txt
# FFmpeg 手动装好后，自检
python ~/.workbuddy/skills/video-ai-cut/verify_skill.py
```

---

## ✅ 装完怎么用

| 平台 | 用法 |
|------|------|
| **WorkBuddy** | 新对话直接说「帮我剪辑这个视频」并拖入视频，或命令行 `python <技能目录>\main.py 视频.mp4` |
| **Codex** | 新会话输入 `$video-ai-cut` 直接调用，或自然描述「剪辑这个视频：加字幕、删停顿、消敏感音」 |
| **命令行** | `python main.py "视频.mp4"` 一键全自动 |

## 🔄 升级到最新版

```bash
git -C ~/.workbuddy/skills/video-ai-cut pull
# Codex 也装了的话
git -C ~/.codex/skills/video-ai-cut pull
```

## 🎬 视频号草稿同步（可选）

```bash
pip install playwright
python main.py sync "成片.mp4" --title "标题" --headed   # 首次扫码，之后免扫码
```

详见 `INSTALL.md`「三.5、视频号草稿同步」章节。
