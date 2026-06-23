"""探针：验证本地 vLLM 端点是否支持 assistant 前缀续写 (continue_final_message)。

判定标准：
  1. 参数被接受（不报 400 unknown parameter）；
  2. 真·续写——输出从给定 assistant 前缀「接着写」，包括把截断的 JSON 补完。

关键：本地 Qwen3.6 是推理模型，thinking 开启时正文走 reasoning_content、content 为空。
agent 主循环默认 enable_thinking=False，所以这里也带上 chat_template_kwargs.enable_thinking=False
复刻真实调用配置，否则会误判为"续写不工作"。

用法： desktop/vendor/python/python.exe debug_tools/probe_continuation.py [main|backup|all]
"""
import os
import sys

os.environ.setdefault("PYTHONUTF8", "1")

# --- 加载 .env（不依赖 python-dotenv）---
ENV = {}
_envpath = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(_envpath):
    for line in open(_envpath, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            ENV[k.strip()] = v.strip()

import openai

NOTHINK = {"chat_template_kwargs": {"enable_thinking": False}}
CONT = {"chat_template_kwargs": {"enable_thinking": False},
        "continue_final_message": True, "add_generation_prompt": False}


def resolve_model(client, configured):
    """若 .env 配的模型名服务器不认，回退到 /models 报告的第一个真实 id。"""
    try:
        ids = [m.id for m in client.models.list().data]
    except Exception:
        return configured
    if configured in ids:
        return configured
    if ids:
        print(f"  [注意] 配置模型 {configured!r} 不在服务端，改用 {ids[0]!r}")
        return ids[0]
    return configured


def probe(name, base_url, api_key, model):
    print(f"\n{'='*60}\n[{name}] {base_url}\n{'='*60}")
    client = openai.OpenAI(base_url=base_url, api_key=api_key, timeout=60, max_retries=0)
    model = resolve_model(client, model)
    print(f"  model = {model}")

    # 截断的 JSON 前缀（贴近真实场景）——应接着把字符串和括号补完
    prefix = '{"thought": "print", "action": "tool_call", "tool": "shell", "args": {"command": "echo'
    msgs = [
        {"role": "user", "content": "Reply with one JSON object for a shell command that prints hello."},
        {"role": "assistant", "content": prefix},
    ]
    try:
        r = client.chat.completions.create(
            model=model, messages=msgs, max_tokens=40, temperature=0, extra_body=CONT,
        )
        out = r.choices[0].message.content or ""
        print(f"  finish_reason = {r.choices[0].finish_reason!r}")
        print(f"  续写输出 = {out!r}")
        import json
        try:
            json.loads(prefix + out)
            print("  [PASS] 前缀 + 续写 = 合法 JSON：续写工作正常")
        except Exception:
            print("  [WARN] 拼接后仍非合法 JSON，需人工判断")
    except Exception as e:
        print(f"  [FAIL] 续写请求被拒: {type(e).__name__}: {str(e)[:300]}")


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    targets = []
    if which in ("main", "all") and ENV.get("OPENAI_BASE_URL"):
        targets.append(("主", ENV["OPENAI_BASE_URL"], ENV.get("OPENAI_API_KEY", "local"), ENV.get("OPENAI_MODEL", "qwen")))
    if which in ("backup", "all") and ENV.get("BACKUP_OPENAI_BASE_URL"):
        targets.append(("备", ENV["BACKUP_OPENAI_BASE_URL"], ENV.get("BACKUP_OPENAI_API_KEY", "local"), ENV.get("BACKUP_OPENAI_MODEL", "qwen36")))
    if not targets:
        print("未找到匹配端点"); sys.exit(1)
    for t in targets:
        probe(*t)


if __name__ == "__main__":
    main()
