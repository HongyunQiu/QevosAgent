"""图片可达性校验（程序化校验器）。

背景：模型在回答（ask_user 的 question / done 的 final_answer）里嵌入 run 内图片时，
常把 src 写成带 runs/<runId>/ 前缀的仓库相对路径、甚至绝对路径，前端 _resolveRunImgSrc
会自动再拼一次 run 目录，导致路径重复 → 404 → 用户看到破图标。

前端 _resolveRunImgSrc 已能静默吞掉无歧义误写（反斜杠、冗余 runs/ 前缀）。
本模块负责前端救不回来的情况：归一后文件仍不存在 / 跨 run 歧义 —— 检测出来，
连同『你是不是想引用 artifacts/xxx.png』的纠正建议一起回传给模型，让它在内容
到达用户眼前之前就把 src 改对（复用 agent 已有的『输出有问题→回传重试』范式）。

normalize_img_src 与 dashboard/public/index.html 的 _resolveRunImgSrc 保持等价规则，
两边改动需同步。
"""
import os
import re

# markdown 图片 ![alt](src) 与 HTML <img ... src="src" ...>
_MD_IMG = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)")
_HTML_IMG = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)


def extract_image_srcs(text):
    """从文本中提取所有图片 src（去重、保序）。"""
    if not text:
        return []
    seen, out = set(), []
    for m in list(_MD_IMG.finditer(text)) + list(_HTML_IMG.finditer(text)):
        s = m.group(1).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def normalize_img_src(src, run_id):
    """把 src 归一成 run 内相对路径。

    返回 (kind, rel)：
      - ("run", rel)  ：rel 是相对该 run 目录的路径，需要检查文件是否存在。
      - ("external", src)：外链 / data / blob / 已是 /api/ / 绝对文件系统路径，
        不归 run 目录管，跳过校验。
    与前端 _resolveRunImgSrc 等价：先剥 ./，绝对路径判 external，
    再剥冗余的 runs/ 与紧跟的当前 runId/ 段。
    """
    if not src:
        return ("external", src)
    if re.match(r"^(https?:|data:|blob:|//|/api/)", src, re.IGNORECASE):
        return ("external", src)
    rel = src[2:] if src.startswith("./") else src
    # 绝对文件系统路径（Windows 盘符 / file: / 以 / 或 \ 开头）→ 前端不服务，跳过
    if re.match(r"^([A-Za-z]:[\\/]|file:|[\\/])", rel):
        return ("external", src)
    if rel.startswith("runs/"):
        rel = rel[5:]
    if run_id and rel.startswith(run_id + "/"):
        rel = rel[len(run_id) + 1:]
    return ("run", rel)


def _find_similar(run_dir, basename, max_depth=3):
    """在 run_dir 下按 basename 找同名文件（限定深度，避免大目录遍历）。"""
    hits = []
    if not basename:
        return hits
    root_depth = run_dir.rstrip("\\/").count(os.sep)
    for cur, dirs, files in os.walk(run_dir):
        if cur.count(os.sep) - root_depth >= max_depth:
            dirs[:] = []
        for f in files:
            if f == basename:
                hits.append(os.path.relpath(os.path.join(cur, f), run_dir).replace("\\", "/"))
    return hits


def check_answer_images(text, run_dir, run_id):
    """校验回答文本里的 run 内图片是否可达。

    返回破图列表，每项 {src, resolved, suggestions:[相对路径]}；全部可达则返回 []。
    external（外链/绝对路径）跳过，不计入破图。
    """
    broken = []
    for src in extract_image_srcs(text):
        kind, rel = normalize_img_src(src, run_id)
        if kind != "run":
            continue
        full = os.path.normpath(os.path.join(run_dir, rel))
        if os.path.isfile(full):
            continue
        suggestions = _find_similar(run_dir, os.path.basename(rel))
        # 去重、排除与当前错误路径相同者
        suggestions = [s for s in dict.fromkeys(suggestions) if s != rel][:5]
        broken.append({"src": src, "resolved": rel, "suggestions": suggestions})
    return broken


def format_feedback(broken):
    """把破图列表拼成回传给模型的反馈消息。"""
    lines = ["[图片校验] 你这条回答里有图片在用户端会显示成破图标，请先修正 src 再重新提交："]
    for b in broken:
        lines.append(f"  - src=`{b['src']}` 解析到 run 目录下的 `{b['resolved']}`，该文件不存在。")
        if b["suggestions"]:
            lines.append(f"    该 run 目录下同名文件其实在：{', '.join('`'+s+'`' for s in b['suggestions'])}")
            lines.append("    请把 src 改成上面之一（相对 run 目录的路径，如 artifacts/xxx.png，不要带 runs/<runId>/ 前缀，也不要写绝对路径）。")
        else:
            lines.append("    该 run 目录下找不到同名文件：请确认图片是否真的落盘、路径是否写对；若尚未生成请先生成再引用。")
    lines.append("修正后重新提交即可；图片正常显示无需再提。")
    return "\n".join(lines)
