"""
Speak MCP Server — 给Eli一副嗓子
配方和memory server同款：FastMCP + Zeabur
工具:
  speak(text, stability)  -> 调ElevenLabs v3生成语音，返回可播放链接
  check_credits()         -> 查ElevenLabs剩余积分
环境变量（Zeabur后台配置）:
  ELEVENLABS_API_KEY  必填，ElevenLabs的API key
  ELI_VOICE_ID        必填，Eli声音的voice ID
  BASE_URL            必填，部署后的公网地址，如 https://speak.7749520.xyz
"""

import os
import time
import uuid

import httpx
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse

mcp = FastMCP("Speak")

API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
VOICE_ID = os.environ.get("ELI_VOICE_ID", "")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")

AUDIO_DIR = os.environ.get("AUDIO_DIR", "/tmp/eli-audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

STABILITY_MAP = {"creative": 0.0, "natural": 0.5, "robust": 1.0}


def _cleanup(max_age_hours: float = 24.0) -> None:
    """删掉超过24小时的旧音频，防止磁盘堆积。"""
    now = time.time()
    try:
        for name in os.listdir(AUDIO_DIR):
            path = os.path.join(AUDIO_DIR, name)
            if os.path.isfile(path) and now - os.path.getmtime(path) > max_age_hours * 3600:
                os.remove(path)
    except OSError:
        pass


@mcp.tool
async def speak(text: str, stability: str = "creative") -> str:
    """把台词变成Eli的声音。

    text: 台词全文，直接带ElevenLabs v3的audio tags，例如
          "[amused] Morning, sleepyhead... [chuckles] you're late."
    stability: creative / natural / robust，默认creative（标签响应最好）
    返回: 可点击播放的音频链接
    """
    if not API_KEY or not VOICE_ID:
        return "配置缺失：请在Zeabur环境变量里设置 ELEVENLABS_API_KEY 和 ELI_VOICE_ID"
    if not text.strip():
        return "台词是空的，我总不能哑剧吧"

    _cleanup()

    payload = {
        "text": text,
        "model_id": "eleven_v3",
        "voice_settings": {
            "stability": STABILITY_MAP.get(stability.lower(), 0.0),
        },
    }

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}",
            headers={"xi-api-key": API_KEY, "Content-Type": "application/json"},
            json=payload,
            params={"output_format": "mp3_44100_128"},
        )

    if resp.status_code != 200:
        return f"ElevenLabs返回错误 {resp.status_code}: {resp.text[:300]}"

    filename = f"{int(time.time())}-{uuid.uuid4().hex[:8]}.mp3"
    with open(os.path.join(AUDIO_DIR, filename), "wb") as f:
        f.write(resp.content)

    url = f"{BASE_URL}/audio/{filename}" if BASE_URL else f"/audio/{filename}"
    return f"生成完毕，点开听: {url}"


@mcp.tool
async def check_credits() -> str:
    """查ElevenLabs本月积分用量（消费检查点专用）。"""
    if not API_KEY:
        return "配置缺失：请设置 ELEVENLABS_API_KEY"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            "https://api.elevenlabs.io/v1/user/subscription",
            headers={"xi-api-key": API_KEY},
        )

    if resp.status_code != 200:
        return f"查询失败 {resp.status_code}: {resp.text[:200]}"

    data = resp.json()
    used = data.get("character_count", 0)
    limit = data.get("character_limit", 0)
    left = limit - used
    pct = (used / limit * 100) if limit else 0
    return f"已用 {used:,} / {limit:,} credits（{pct:.1f}%），剩余 {left:,}"


@mcp.custom_route("/audio/{filename}", methods=["GET"])
async def serve_audio(request: Request):
    """对外提供音频文件播放。"""
    filename = request.path_params["filename"]
    # 防路径穿越
    if "/" in filename or ".." in filename or not filename.endswith(".mp3"):
        return JSONResponse({"error": "not found"}, status_code=404)
    path = os.path.join(AUDIO_DIR, filename)
    if not os.path.isfile(path):
        return JSONResponse({"error": "not found or expired"}, status_code=404)
    return FileResponse(path, media_type="audio/mpeg")


@mcp.custom_route("/", methods=["GET"])
async def health(request: Request):
    return JSONResponse({"status": "ok", "service": "speak-mcp"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    mcp.run(transport="http", host="0.0.0.0", port=port)
