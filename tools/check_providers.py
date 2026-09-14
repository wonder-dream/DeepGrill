"""一次性：确认三个供应商到底配通了没有（用完可删 —— 放在 `tools/` 就是这个意思）。

它回答两个问题，分开答，因为它们的失败方式不同：

1. **配了吗**：三个供应商各自的 provider / model / base_url 读出来是什么（不打印密钥）
2. **通吗**（`--live`）：真调一次 —— 嵌入选几段中文，看维数与耗时；转写喂一段
   音频（`--audio <文件>`，没有就现场合成一段"喂，能听见吗"的 WAV），看它听出了什么

用法：
    python tools/check_providers.py              # 只看配置
    python tools/check_providers.py --live       # 真调嵌入（免费）
    python tools/check_providers.py --live --audio 有人说的话.wav
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

#: 用来测嵌入的三段中文 —— 一段是技术问题、一段是它的近义改写、一段完全无关。
#: **看的不只是"通没通"，还有"近义的是不是比无关的更近"**：那才是语义嵌入。
SAMPLES = [
    "volatile 为什么能保证可见性？底层靠什么实现？",
    "volatile 的可见性是怎么做到的，底层机制是什么？",
    "你负责的那个项目里，缓存和数据库的更新顺序是怎么定的？",
]


def _say(text: str) -> Path:
    """用 Windows 自带的语音合成造一段 WAV —— 这样测转写不必去外面找音频。

    ⚠️ 走**脚本文件**而不是 `-Command "<一长串>"`：`SetOutputToWaveFile($args[0])`
    里的引号、`$`、中文在多层引号里会被吃掉（实测拼命令行失败过一次）。
    用 `powershell.exe`（Windows PowerShell 5.1）而不是 `pwsh`：`System.Speech`
    在那边一定在。
    """
    import subprocess

    work = ROOT / ".tmp" / "provider-check"
    work.mkdir(parents=True, exist_ok=True)
    script = work / "say.ps1"
    script.write_text(
        "Add-Type -AssemblyName System.Speech\n"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
        "$s.SetOutputToWaveFile($args[0])\n"
        "$s.Speak($args[1])\n"
        "$s.Dispose()\n",
        encoding="utf-8",
    )
    out = work / "speech.wav"
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(script), str(out), text],
        check=True,
        capture_output=True,
    )
    return out


def _silence(path: Path, seconds: float = 1.0) -> Path:
    """没有 TTS 时的兜底：一段 16kHz 单声道的静音 WAV。

    它测不出"识别得对不对"，但能测出**请求形状**对不对（multipart、模型名、
    鉴权、返回体）—— 静音被转写成空文本时我们的客户端会明确报错，那也是有信息的。
    """
    rate = 16000
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<h", 0) * int(rate * seconds))
    return path


def main(argv: list[str] | None = None) -> int:
    from app.config import Settings
    from app.deps import get_embeddings, get_stt

    parser = argparse.ArgumentParser(prog="python tools/check_providers.py")
    parser.add_argument("--live", action="store_true", help="真调一次（嵌入免费）")
    parser.add_argument("--audio", default="", help="转写用的音频文件（wav/mp3/webm）")
    parser.add_argument("--say", default="喂，能听见吗？这是一段测试语音。",
                        help="没有 --audio 时用系统 TTS 合成的这句话")
    args = parser.parse_args(argv)

    settings = Settings()
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    print("配置（密钥只显示有没有）：")
    for name in ("llm", "embedding", "stt"):
        provider = getattr(settings, f"{name}_provider", "llm")
        model = getattr(settings, f"model_{name}", "") or getattr(settings, f"{name}_model", "")
        base = getattr(settings, f"{name}_base_url", "") or (
            settings.llm_base_url if name in ("llm", "embedding", "stt") else ""
        )
        key = getattr(settings, f"{name}_api_key", "")
        # ⚠️ 回落是**代码里的行为**（同一家服务商时少配两个变量），但报告里必须分开写：
        # 不然「用了 DeepSeek 的 key 去打硅基流动」会显示成"配好了"，而真实结果是 401。
        own = "有" if key else ""
        fallback = "回落用 LLM 的 key" if (not key and settings.llm_api_key) else ""
        print(
            f"  {name:<10} provider={provider:<6} model={model or '<空>':<34} "
            f"base={base or '<空>':<30} key={own or fallback or '**没有**'}"
        )

    if not args.live:
        print("\n（加 --live 才会真的调一次）")
        return 0

    failed = False

    print("\n嵌入：")
    if settings.embedding_provider != "api":
        print("  ⏭ 没配成 api，跳过")
    else:
        try:
            client = get_embeddings(settings)
            started = time.time()
            vectors = client.embed(SAMPLES)
            elapsed = time.time() - started
            print(f"  ✅ {len(vectors)} 条，维数 {len(vectors[0])}，耗时 {elapsed:.2f}s")
            cosine = getattr(client, "cosine", None) or _cosine
            print(f"     近义（1↔2）{cosine(vectors[0], vectors[1]):.4f}  "
                  f"无关（1↔3）{cosine(vectors[0], vectors[2]):.4f}")
            print("     ↑ 近义的必须明显高于无关的 —— 否则拿到的不是语义嵌入")
        except Exception as e:  # noqa: BLE001 —— 一次性脚本：把失败打出来就是它的用途
            failed = True
            print(f"  ❌ {type(e).__name__}: {e}")

    print("\n语音转写：")
    if settings.stt_provider != "api":
        print("  ⏭ 没配成 api，跳过")
    else:
        try:
            if args.audio:
                audio = Path(args.audio)
                data = audio.read_bytes()
                content_type = {
                    ".wav": "audio/wav", ".mp3": "audio/mpeg",
                    ".webm": "audio/webm", ".ogg": "audio/ogg", ".m4a": "audio/mp4",
                }.get(audio.suffix.lower(), "audio/wav")
                print(f"  音频：{audio.name}（{len(data)} 字节，{content_type}）")
            else:
                try:
                    audio = _say(args.say)
                    print(f"  音频：系统 TTS 合成的「{args.say}」")
                except Exception:  # TTS 不可用 → 静音兜底
                    audio = _silence(ROOT / ".tmp" / "provider-check" / "silence.wav")
                    print("  音频：合成失败，改用 1 秒静音（只测请求形状）")
                data = audio.read_bytes()
                content_type = "audio/wav"
            started = time.time()
            result = get_stt(settings).transcribe(data, content_type=content_type)
            print(f"  ✅ 转写（{time.time() - started:.2f}s）：{result.text}")
        except Exception as e:  # noqa: BLE001
            failed = True
            print(f"  ❌ {type(e).__name__}: {e}")

    return 1 if failed else 0


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


if __name__ == "__main__":
    raise SystemExit(main())
