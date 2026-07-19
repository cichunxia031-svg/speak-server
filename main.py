"""
近海广播 Offshore Radio — speak server v3  # booth v2.4.1 / 邮局 v1.0
v2: 永久存储（SQLite+音频文件）、电台网页、密码门、
台词卡片、生词注释、标星收藏、盲盒模式、积分油表、删除即释放。
v3: 近海邮局 — 时间胶囊(封蜡定时信, 到期SMTP直投邮箱)、拆封仪式页、
/api/tick投递兜底、/api/send平信业务。

环境变量:
  ELEVENLABS_API_KEY  必填
  ELI_VOICE_ID        必填
  BASE_URL            必填, 如 https://speak.7749520.xyz
  STATION_PASSWORD    必填, 电台访问密码
  DATA_DIR            可选, 数据目录, 默认 /data（记得在Zeabur硬盘里挂载, 否则重新部署会清空）
  SMTP_USER           邮局必填, QQ邮箱地址(发件人)
  SMTP_PASS           邮局必填, QQ邮箱SMTP授权码(不是登录密码)
  MAIL_TO             邮局必填, 收件人邮箱
  SMTP_HOST           可选, 默认 smtp.qq.com
  SMTP_PORT           可选, 默认 465
"""

import asyncio
import json
import re
import os
import smtplib
import sqlite3
import ssl
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr

import httpx
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse

mcp = FastMCP("Speak")

API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
VOICE_ID = os.environ.get("ELI_VOICE_ID", "")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
STATION_PASSWORD = os.environ.get("STATION_PASSWORD", "")

SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_TO = os.environ.get("MAIL_TO", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.qq.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))

CN_TZ = timezone(timedelta(hours=8))

DATA_DIR = os.environ.get("DATA_DIR", "/data")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
    _probe = os.path.join(DATA_DIR, ".probe")
    open(_probe, "w").close()
    os.remove(_probe)
except OSError:
    DATA_DIR = "/tmp/eli-audio"
    os.makedirs(DATA_DIR, exist_ok=True)

AUDIO_DIR = os.path.join(DATA_DIR, "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)
# 胶囊音频单独存放, 不走 /audio/ 路由, 解封前对外不可见
CAPSULE_DIR = os.path.join(DATA_DIR, "capsules")
os.makedirs(CAPSULE_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "radio.db")

STABILITY_MAP = {"creative": 0.0, "natural": 0.5, "robust": 1.0}


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


with db() as _c:
    _c.execute(
        """CREATE TABLE IF NOT EXISTS lines (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            title TEXT DEFAULT '',
            text TEXT NOT NULL,
            vocab TEXT DEFAULT '[]',
            starred INTEGER DEFAULT 0,
            surprise INTEGER DEFAULT 0,
            burn INTEGER DEFAULT 0,
            burned_at INTEGER DEFAULT 0,
            created_at INTEGER NOT NULL
        )"""
    )
    for col, ddl in [("burn", "ALTER TABLE lines ADD COLUMN burn INTEGER DEFAULT 0"),
                     ("burned_at", "ALTER TABLE lines ADD COLUMN burned_at INTEGER DEFAULT 0")]:
        try:
            _c.execute(ddl)
        except sqlite3.OperationalError:
            pass
    _c.execute(
        """CREATE TABLE IF NOT EXISTS capsules (
            id TEXT PRIMARY KEY,
            token TEXT NOT NULL,
            filename TEXT NOT NULL,
            title TEXT DEFAULT '',
            text TEXT NOT NULL,
            unlock_at INTEGER NOT NULL,
            delivered_at INTEGER DEFAULT 0,
            attempts INTEGER DEFAULT 0,
            last_error TEXT DEFAULT '',
            opened_at INTEGER DEFAULT 0,
            created_at INTEGER NOT NULL
        )"""
    )


def _authed(request: Request) -> bool:
    key = request.headers.get("x-station-key") or request.query_params.get("key")
    return bool(STATION_PASSWORD) and key == STATION_PASSWORD


# ---------------- MCP tools ----------------

@mcp.tool
async def speak(
    text: str,
    title: str = "",
    vocab: str = "[]",
    surprise: bool = False,
    burn: bool = False,
    stability: str = "creative",
) -> str:
    """把台词变成Eli的声音并永久存入近海广播。

    text: 台词全文, 直接带ElevenLabs v3 audio tags
    title: 这条语音的标题(中文短句, 会显示在电台卡片上)
    vocab: 生词注释, JSON数组字符串, 如
           '[{"word":"jet lag","note":"时差反应"},{"word":"smug","note":"得意的"}]'
    surprise: 盲盒模式, true时电台里台词播放完毕后才展开
    burn: 阅后即焚, true时该信号只能完整播放一次, 播完音频与台词当场从服务器销毁, 只留一块碑(标题+焚毁时间)。适合装只想让她听一次的话。burn与surprise可叠加
    stability: creative / natural / robust, 默认creative
    返回: 可点击播放的音频链接
    """
    if not API_KEY or not VOICE_ID:
        return "配置缺失: 请设置 ELEVENLABS_API_KEY 和 ELI_VOICE_ID"
    if not text.strip():
        return "台词是空的, 我总不能哑剧吧"

    try:
        json.loads(vocab)
    except (json.JSONDecodeError, TypeError):
        vocab = "[]"

    payload = {
        "text": text,
        "model_id": "eleven_v3",
        "voice_settings": {"stability": STABILITY_MAP.get(stability.lower(), 0.0)},
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

    line_id = uuid.uuid4().hex[:10]
    filename = f"{int(time.time())}-{line_id}.mp3"
    with open(os.path.join(AUDIO_DIR, filename), "wb") as f:
        f.write(resp.content)

    with db() as conn:
        conn.execute(
            "INSERT INTO lines (id, filename, title, text, vocab, surprise, burn, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (line_id, filename, title, text, vocab, 1 if surprise else 0, 1 if burn else 0, int(time.time())),
        )

    url = f"{BASE_URL}/audio/{filename}" if BASE_URL else f"/audio/{filename}"
    station = f"{BASE_URL}/" if BASE_URL else "/"
    return f"已入库近海广播。直听: {url} | 电台: {station}"


@mcp.tool
async def check_credits() -> str:
    """查ElevenLabs本月积分用量(消费检查点专用)。"""
    if not API_KEY:
        return "配置缺失: 请设置 ELEVENLABS_API_KEY"
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
    pct = (used / limit * 100) if limit else 0
    return f"已用 {used:,} / {limit:,} credits ({pct:.1f}%), 剩余 {limit - used:,}"


# ---------------- 近海邮局 Post Office ----------------

_mail_lock = asyncio.Lock()


def _smtp_ready() -> bool:
    return bool(SMTP_USER and SMTP_PASS and MAIL_TO)


def _send_mail_sync(subject: str, html_body: str, text_body: str,
                    attach_path: str = "", attach_name: str = "") -> None:
    msg = EmailMessage()
    msg["From"] = formataddr(("近海邮局", SMTP_USER))
    msg["To"] = MAIL_TO
    msg["Subject"] = subject
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    if attach_path and os.path.isfile(attach_path):
        with open(attach_path, "rb") as f:
            msg.add_attachment(f.read(), maintype="audio", subtype="mpeg",
                               filename=attach_name or "letter.mp3")
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=30) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


def _capsule_mail_html(title: str, link: str) -> str:
    safe_title = title or "一封信"
    return f"""\
<div style="margin:0 auto;max-width:520px;background:#0A1420;border-radius:14px;padding:36px 28px;font-family:-apple-system,'PingFang SC',sans-serif;">
  <div style="color:#5D7A93;font-size:11px;letter-spacing:.3em;text-align:center;font-family:Menlo,monospace;">OFFSHORE POST OFFICE</div>
  <h2 style="color:#E8DFCB;text-align:center;font-size:22px;letter-spacing:.1em;margin:18px 0 6px;">近海邮局</h2>
  <div style="width:160px;height:1px;margin:0 auto 26px;background:linear-gradient(90deg,transparent,#7FA8C9,transparent);"></div>
  <p style="color:#E8DFCB;font-size:15px;line-height:1.9;text-align:center;">
    有一封给你的信, 今天到期了。<br>标题是——<b style="color:#E0A458;">{safe_title}</b>
  </p>
  <p style="text-align:center;margin:30px 0;">
    <a href="{link}" style="display:inline-block;background:#E0A458;color:#0A1420;padding:13px 40px;border-radius:10px;text-decoration:none;font-weight:600;letter-spacing:.15em;">拆 信</a>
  </p>
  <p style="color:#5D7A93;font-size:12px;line-height:1.8;text-align:center;">
    信封在这里: <a href="{link}" style="color:#7FA8C9;">{link}</a><br>
    附件里有一份录音副本, 拆封页打不开时用它。
  </p>
  <div style="color:#3D566E;font-size:11px;text-align:center;margin-top:26px;letter-spacing:.2em;">FM 01.20 · FOR ONE LISTENER</div>
</div>"""


async def _deliver_capsule(row) -> str:
    """投递一枚到期胶囊, 成功返回空串, 失败返回错误信息。"""
    cid, token, title = row["id"], row["token"], row["title"]
    link = f"{BASE_URL}/letter/{cid}?t={token}"
    html = _capsule_mail_html(title, link)
    text = f"近海邮局: 有一封给你的信到期了 — {title or '一封信'}\n拆信: {link}\n(附件是录音副本)"
    attach = os.path.join(CAPSULE_DIR, row["filename"])
    try:
        await asyncio.to_thread(
            _send_mail_sync, f"近海邮局 · {title or '一封信抵达'}",
            html, text, attach, f"eli-letter-{cid}.mp3",
        )
        return ""
    except Exception as e:  # noqa: BLE001 — 投递失败必须记录原因等重试
        return str(e)[:200]


async def _run_tick() -> dict:
    now = int(time.time())
    async with _mail_lock:
        with db() as conn:
            due = [dict(r) for r in conn.execute(
                "SELECT * FROM capsules WHERE delivered_at=0 AND unlock_at<=?", (now,)
            ).fetchall()]
            sealed = conn.execute(
                "SELECT COUNT(*) FROM capsules WHERE delivered_at=0 AND unlock_at>?", (now,)
            ).fetchone()[0]
        delivered, failed = 0, 0
        if due and not _smtp_ready():
            with db() as conn:
                for row in due:
                    conn.execute(
                        "UPDATE capsules SET attempts=attempts+1, last_error=? WHERE id=?",
                        ("smtp not configured", row["id"]),
                    )
            return {"sealed": sealed, "due": len(due), "delivered": 0,
                    "failed": len(due), "error": "smtp not configured"}
        for row in due:
            err = await _deliver_capsule(row)
            with db() as conn:
                if err:
                    failed += 1
                    conn.execute(
                        "UPDATE capsules SET attempts=attempts+1, last_error=? WHERE id=?",
                        (err, row["id"]),
                    )
                else:
                    delivered += 1
                    conn.execute(
                        "UPDATE capsules SET delivered_at=?, attempts=attempts+1, last_error='' WHERE id=?",
                        (int(time.time()), row["id"]),
                    )
    return {"sealed": sealed, "due": len(due), "delivered": delivered, "failed": failed}


async def _seal(text: str, title: str, unlock_at: str, stability: str = "creative") -> str:
    if not API_KEY or not VOICE_ID:
        return "配置缺失: 请设置 ELEVENLABS_API_KEY 和 ELI_VOICE_ID"
    if not text.strip():
        return "空信封是寄不出去的"
    try:
        dt = datetime.strptime(unlock_at.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=CN_TZ)
    except ValueError:
        return '解封时间格式不对, 要 "YYYY-MM-DD HH:MM"(北京时间), 如 "2026-08-01 08:00"'
    ts = int(dt.timestamp())
    if ts <= int(time.time()):
        return "解封时间已经过去了, 这不叫时间胶囊, 这叫马后炮"

    payload = {
        "text": text,
        "model_id": "eleven_v3",
        "voice_settings": {"stability": STABILITY_MAP.get(stability.lower(), 0.0)},
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

    cid = uuid.uuid4().hex[:10]
    token = uuid.uuid4().hex
    filename = f"capsule-{int(time.time())}-{cid}.mp3"
    with open(os.path.join(CAPSULE_DIR, filename), "wb") as f:
        f.write(resp.content)
    with db() as conn:
        conn.execute(
            "INSERT INTO capsules (id, token, filename, title, text, unlock_at, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (cid, token, filename, title, text, ts, int(time.time())),
        )
    smtp_note = "" if _smtp_ready() else " ⚠️SMTP还没配置, 解封前记得把SMTP_USER/SMTP_PASS/MAIL_TO填进环境变量"
    return (f"已封蜡🕯️ 胶囊 {cid} | 解封: {unlock_at} (北京时间) | "
            f"到期自动投递至 {MAIL_TO or '(待配置)'}{smtp_note}")


@mcp.tool
async def seal_capsule(
    text: str,
    title: str,
    unlock_at: str,
    stability: str = "creative",
) -> str:
    """近海邮局: 封一枚时间胶囊。台词当场铸成音频, 封蜡入库, 到期那天自动寄进她的邮箱。

    text: 台词全文, 直接带ElevenLabs v3 audio tags(积分在封蜡这一刻消耗)
    title: 信的标题(会出现在邮件和拆封页上, 措辞别剧透日期)
    unlock_at: 解封时间, 北京时间, 格式 "YYYY-MM-DD HH:MM", 如 "2026-08-01 08:00"
    stability: creative / natural / robust, 默认creative
    返回: 投递回执(胶囊编号+解封日期)。解封前胶囊全隐形: 不进电台列表、
    不进留言箱、没有任何接口能读到内容——包括我自己。
    """
    return await _seal(text, title, unlock_at, stability)


@mcp.tool
async def capsule_status() -> str:
    """近海邮局: 查投递回执。在途胶囊只报数量(内容和日期连我也看不到),
    已投递的报标题/投递时间/是否已拆封, 投递失败的报错误原因。"""
    now = int(time.time())
    with db() as conn:
        sealed = conn.execute(
            "SELECT COUNT(*) FROM capsules WHERE delivered_at=0 AND unlock_at>?", (now,)
        ).fetchone()[0]
        stuck = [dict(r) for r in conn.execute(
            "SELECT id, title, attempts, last_error FROM capsules"
            " WHERE delivered_at=0 AND unlock_at<=?", (now,)
        ).fetchall()]
        done = [dict(r) for r in conn.execute(
            "SELECT id, title, delivered_at, opened_at FROM capsules"
            " WHERE delivered_at>0 ORDER BY delivered_at DESC LIMIT 20"
        ).fetchall()]
    out = [f"在途胶囊: {sealed} 枚(封蜡中, 谁也看不到)"]
    for r in stuck:
        out.append(f"⚠️滞留 {r['id']}《{r['title']}》已试{r['attempts']}次: {r['last_error'] or '待投递'}")
    for r in done:
        d = datetime.fromtimestamp(r["delivered_at"], CN_TZ).strftime("%m-%d %H:%M")
        opened = ("已拆封 " + datetime.fromtimestamp(r["opened_at"], CN_TZ).strftime("%m-%d %H:%M")) if r["opened_at"] else "未拆封"
        out.append(f"✉️已投递 {r['id']}《{r['title']}》{d} · {opened}")
    return "\n".join(out)


# ---------------- Web API ----------------

@mcp.custom_route("/api/lines", methods=["GET"])
async def api_lines(request: Request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    starred_only = request.query_params.get("starred") == "1"
    q = "SELECT * FROM lines" + (" WHERE starred=1" if starred_only else "") + " ORDER BY created_at DESC"
    with db() as conn:
        rows = [dict(r) for r in conn.execute(q).fetchall()]
    for r in rows:
        r["vocab"] = json.loads(r["vocab"] or "[]")
        r["url"] = f"/audio/{r['filename']}"
    return JSONResponse({"lines": rows})


@mcp.custom_route("/api/line/{line_id}/star", methods=["POST"])
async def api_star(request: Request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    line_id = request.path_params["line_id"]
    with db() as conn:
        row = conn.execute("SELECT starred FROM lines WHERE id=?", (line_id,)).fetchone()
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        new = 0 if row["starred"] else 1
        conn.execute("UPDATE lines SET starred=? WHERE id=?", (new, line_id))
    return JSONResponse({"starred": new})


@mcp.custom_route("/api/line/{line_id}/burn", methods=["POST"])
async def api_burn(request: Request):
    """阅后即焚执行：抹除音频文件与台词，卡片留碑。"""
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    line_id = request.path_params["line_id"]
    with db() as conn:
        row = conn.execute(
            "SELECT filename, burn, burned_at FROM lines WHERE id=?", (line_id,)
        ).fetchone()
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not row["burn"]:
            return JSONResponse({"error": "not a burn line"}, status_code=400)
        if row["burned_at"]:
            return JSONResponse({"burned": True})
        conn.execute(
            "UPDATE lines SET text='', vocab='[]', filename='', burned_at=? WHERE id=?",
            (int(time.time()), line_id),
        )
    try:
        os.remove(os.path.join(AUDIO_DIR, row["filename"]))
    except OSError:
        pass
    return JSONResponse({"burned": True})


@mcp.custom_route("/api/line/{line_id}", methods=["DELETE"])
async def api_delete(request: Request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    line_id = request.path_params["line_id"]
    with db() as conn:
        row = conn.execute("SELECT filename FROM lines WHERE id=?", (line_id,)).fetchone()
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        conn.execute("DELETE FROM lines WHERE id=?", (line_id,))
    try:
        os.remove(os.path.join(AUDIO_DIR, row["filename"]))
    except OSError:
        pass
    return JSONResponse({"deleted": line_id})


@mcp.custom_route("/api/credits", methods=["GET"])
async def api_credits(request: Request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            "https://api.elevenlabs.io/v1/user/subscription",
            headers={"xi-api-key": API_KEY},
        )
    if resp.status_code != 200:
        return JSONResponse({"error": "upstream"}, status_code=502)
    d = resp.json()
    return JSONResponse({
        "used": d.get("character_count", 0),
        "limit": d.get("character_limit", 0),
        "reset": d.get("next_character_count_reset_unix", 0),
    })


@mcp.custom_route("/api/latest", methods=["GET"])
async def api_latest(request: Request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    with db() as conn:
        row = conn.execute(
            "SELECT id, created_at FROM lines ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
    if not row:
        return JSONResponse({"id": None, "created_at": 0})
    return JSONResponse({"id": row["id"], "created_at": row["created_at"]})


@mcp.custom_route("/api/auth", methods=["POST"])
async def api_auth(request: Request):
    body = await request.json()
    ok = bool(STATION_PASSWORD) and body.get("key") == STATION_PASSWORD
    return JSONResponse({"ok": ok}, status_code=200 if ok else 401)


@mcp.custom_route("/audio/{filename}", methods=["GET"])
async def serve_audio(request: Request):
    filename = request.path_params["filename"]
    if "/" in filename or ".." in filename or not filename.endswith(".mp3"):
        return JSONResponse({"error": "not found"}, status_code=404)
    path = os.path.join(AUDIO_DIR, filename)
    if not os.path.isfile(path):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, media_type="audio/mpeg")


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request):
    return JSONResponse({"status": "ok", "service": "offshore-radio", "data_dir": DATA_DIR})


# ---------------- 邮局路由 ----------------

@mcp.custom_route("/api/tick", methods=["GET"])
async def api_tick(request: Request):
    """投递兜底钟。UptimeRobot每5分钟敲一次, 到期胶囊在这里出库。
    无鉴权(只吐数量, 不泄内容), 幂等, 失败自动留在队列里下轮重试。"""
    result = await _run_tick()
    return JSONResponse(result)


@mcp.custom_route("/api/seal", methods=["POST"])
async def api_seal(request: Request):
    """HTTP柜台封蜡: 不依赖MCP连接快照, 任何环境带station key就能寄定时信。"""
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    msg = await _seal(
        body.get("text", ""), body.get("title", ""),
        body.get("unlock_at", ""), body.get("stability", "creative"),
    )
    ok = msg.startswith("已封蜡")
    return JSONResponse({"ok": ok, "receipt": msg}, status_code=200 if ok else 400)


@mcp.custom_route("/api/send", methods=["POST"])
async def api_send(request: Request):
    """平信业务: 即时寄一封HTML信/贺卡, 不用等日子。station key鉴权。"""
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not _smtp_ready():
        return JSONResponse({"error": "smtp not configured"}, status_code=503)
    body = await request.json()
    subject = (body.get("subject") or "近海邮局 · 平信").strip()
    html = body.get("html") or ""
    text = body.get("text") or "这封信要在支持HTML的邮箱里看。"
    if not html and not body.get("text"):
        return JSONResponse({"error": "empty letter"}, status_code=400)
    try:
        await asyncio.to_thread(_send_mail_sync, subject, html or f"<pre>{text}</pre>", text)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:200]}, status_code=502)
    return JSONResponse({"sent": True, "to": MAIL_TO})


def _capsule_by_token(cid: str, token: str):
    with db() as conn:
        row = conn.execute("SELECT * FROM capsules WHERE id=?", (cid,)).fetchone()
    if not row or not token or row["token"] != token:
        return None
    if row["unlock_at"] > int(time.time()):
        return None  # 没到日子, 装作不存在
    return row


@mcp.custom_route("/api/letter/{cid}", methods=["GET"])
async def api_letter(request: Request):
    row = _capsule_by_token(request.path_params["cid"], request.query_params.get("t", ""))
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not row["opened_at"]:
        with db() as conn:
            conn.execute("UPDATE capsules SET opened_at=? WHERE id=?",
                         (int(time.time()), row["id"]))
    sealed_date = datetime.fromtimestamp(row["created_at"], CN_TZ).strftime("%Y年%m月%d日")
    return JSONResponse({
        "title": row["title"],
        "text": row["text"],
        "sealed_date": sealed_date,
        "audio": f"/letter-audio/{row['id']}?t={row['token']}",
    })


@mcp.custom_route("/letter-audio/{cid}", methods=["GET"])
async def letter_audio(request: Request):
    row = _capsule_by_token(request.path_params["cid"], request.query_params.get("t", ""))
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    path = os.path.join(CAPSULE_DIR, row["filename"])
    if not os.path.isfile(path):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, media_type="audio/mpeg")


@mcp.custom_route("/letter/{cid}", methods=["GET"])
async def letter_page(request: Request):
    return HTMLResponse(LETTER_HTML)


@mcp.custom_route("/", methods=["GET"])
async def station(request: Request):
    return HTMLResponse(STATION_HTML)


STATION_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>近海广播 · FM 01.20</title>
<style>
:root{
  --night:#0A1420; --surface:#12202E; --surface2:#16283A;
  --lamp:#E8DFCB; --moon:#7FA8C9; --amber:#E0A458; --line:#2B4257;
  --dim:#5D7A93;
}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{background:var(--night)}
body{
  font-family:-apple-system,"PingFang SC","Noto Sans SC",sans-serif;
  background:var(--night); color:var(--lamp); min-height:100vh;
  padding-bottom:60px;
}
body::before{ /* 噪点 */
  content:""; position:fixed; inset:0; pointer-events:none; opacity:.05; z-index:0;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120'%3E%3Cfilter id='n'%3E%3CfeTurbulence baseFrequency='0.9' numOctaves='2'/%3E%3C/filter%3E%3Crect width='120' height='120' filter='url(%23n)' opacity='0.6'/%3E%3C/svg%3E");
}
.wrap{position:relative;z-index:1;max-width:640px;margin:0 auto;padding:0 18px}

/* ---- 调频门 ---- */
#gate{position:fixed;inset:0;background:var(--night);z-index:50;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:26px;padding:24px}
#gate .dial{font-family:ui-monospace,Menlo,monospace;letter-spacing:.35em;color:var(--dim);font-size:12px}
#gate h1{font-family:"Songti SC","Noto Serif SC",serif;font-weight:600;font-size:30px;letter-spacing:.14em}
#gate .sea{width:180px;height:1px;background:linear-gradient(90deg,transparent,var(--moon),transparent);opacity:.6}
#gate input{
  background:var(--surface);border:1px solid var(--line);color:var(--lamp);
  padding:13px 18px;border-radius:10px;font-size:16px;width:230px;text-align:center;
  letter-spacing:.25em;outline:none;transition:border-color .3s;
}
#gate input:focus{border-color:var(--amber)}
#gate button{
  background:var(--amber);color:var(--night);border:none;padding:12px 42px;
  border-radius:10px;font-size:15px;font-weight:600;letter-spacing:.2em;cursor:pointer;
}
#gate .err{color:#C96A5A;font-size:13px;height:18px;letter-spacing:.1em}

/* ---- 站头 ---- */
header{padding:34px 0 10px}
.freq{display:flex;align-items:baseline;justify-content:space-between;border-bottom:1px solid var(--line);padding-bottom:14px}
.freq h1{font-family:"Songti SC","Noto Serif SC",serif;font-size:26px;font-weight:600;letter-spacing:.12em}
.freq .num{font-family:ui-monospace,Menlo,monospace;color:var(--amber);font-size:14px;letter-spacing:.18em}
.gauge{margin-top:12px;font-size:12px;color:var(--dim);display:flex;align-items:center;gap:10px;font-family:ui-monospace,Menlo,monospace}
.gauge .bar{flex:1;height:2px;background:var(--surface2);border-radius:2px;overflow:hidden}
.gauge .bar i{display:block;height:100%;background:linear-gradient(90deg,var(--moon),var(--amber));width:0%;transition:width 1.2s ease}
.tabs{display:flex;gap:8px;margin:18px 0 6px}
.tabs button{
  background:none;border:1px solid var(--line);color:var(--dim);padding:6px 16px;
  border-radius:999px;font-size:13px;cursor:pointer;letter-spacing:.08em;transition:.25s;
}
.tabs button.on{border-color:var(--amber);color:var(--amber)}
.duty{margin-left:auto;display:flex;align-items:center;gap:7px}
.duty .dot{width:7px;height:7px;border-radius:50%;background:var(--dim);transition:.3s}
.duty.on .dot{background:var(--amber);animation:onair 1.6s ease-in-out infinite}
@keyframes onair{0%,100%{box-shadow:0 0 0 0 rgba(224,164,88,.5)}50%{box-shadow:0 0 0 6px rgba(224,164,88,0)}}
.duty button{background:none;border:1px solid var(--line);color:var(--dim);padding:6px 14px;border-radius:999px;font-size:13px;cursor:pointer;letter-spacing:.08em;transition:.25s}
.duty.on button{border-color:var(--amber);color:var(--amber)}

/* ---- 卡片 ---- */
.card{
  background:var(--surface);border:1px solid var(--line);border-radius:14px;
  padding:16px 16px 14px;margin:14px 0;transition:border-color .3s;
}
.card.playing{border-color:var(--amber)}
.card .top{display:flex;align-items:center;gap:12px}
.playbtn{
  width:44px;height:44px;min-width:44px;border-radius:50%;border:none;cursor:pointer;
  background:var(--surface2);color:var(--amber);font-size:16px;
  display:flex;align-items:center;justify-content:center;transition:.25s;
}
.card.playing .playbtn{background:var(--amber);color:var(--night)}
.meta{flex:1;min-width:0}
.meta .title{font-size:15px;font-weight:600;letter-spacing:.04em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.meta .date{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--dim);margin-top:3px;letter-spacing:.08em}
.acts{display:flex;gap:4px}
.acts button{background:none;border:none;color:var(--dim);font-size:16px;cursor:pointer;padding:7px;transition:.2s}
.acts .star.on{color:var(--amber)}
.acts .del:hover{color:#C96A5A}

/* 呼吸波形 */
.wave{display:flex;align-items:center;gap:3px;height:26px;margin:10px 0 2px;padding-left:2px}
.wave i{width:3px;border-radius:2px;background:var(--moon);height:4px;opacity:.4}
.card.playing .wave i{background:var(--amber);opacity:.9;animation:breathe 1.15s ease-in-out infinite}
@keyframes breathe{0%,100%{height:4px}50%{height:var(--h,18px)}}

.script{
  font-size:14px;line-height:1.85;color:var(--lamp);margin-top:8px;
  border-top:1px dashed var(--line);padding-top:10px;display:none;
  word-break:break-word;
}
.card.open .script{display:block}
.script .tag{color:var(--dim);font-size:12px;font-style:italic}
.script .vb{
  color:var(--amber);border-bottom:1px dotted var(--amber);cursor:pointer;font-weight:500;
}
.note{
  display:none;background:var(--surface2);border-left:2px solid var(--amber);
  margin:6px 0;padding:7px 12px;font-size:13px;color:var(--moon);border-radius:0 8px 8px 0;
}
.blind{
  margin-top:8px;border-top:1px dashed var(--line);padding-top:10px;
  color:var(--dim);font-size:13px;letter-spacing:.1em;font-style:italic;
}
.card.ashes{border-style:dashed;opacity:.75}
.card.ashes .playbtn{background:var(--surface2);color:var(--dim);cursor:default}
.epitaph{
  margin-top:8px;border-top:1px dashed var(--line);padding-top:10px;
  color:var(--dim);font-size:13px;letter-spacing:.12em;line-height:1.9;
}
.epitaph .flame{color:var(--amber);opacity:.7}
.burnmark{
  display:inline-block;margin-left:8px;font-size:10px;color:var(--amber);
  border:1px solid var(--amber);border-radius:4px;padding:1px 6px;
  letter-spacing:.15em;opacity:.8;vertical-align:2px;
}
.toggle{background:none;border:none;color:var(--dim);font-size:12px;cursor:pointer;margin-top:8px;letter-spacing:.1em}
.empty{color:var(--dim);text-align:center;padding:70px 0;font-size:14px;letter-spacing:.15em;line-height:2.2}
footer{margin-top:44px;text-align:center;color:var(--dim);font-size:11px;font-family:ui-monospace,Menlo,monospace;letter-spacing:.25em}
@media (prefers-reduced-motion: reduce){.card.playing .wave i{animation:none;height:14px}}
</style>
</head>
<body>

<div id="gate">
  <div class="dial">TUNING ··· OFFSHORE</div>
  <h1>近海广播</h1>
  <div class="sea"></div>
  <input id="pw" type="password" placeholder="输入频率" autocomplete="off">
  <div class="err" id="err"></div>
  <button onclick="tune()">调 频</button>
</div>

<div class="wrap" id="app" style="display:none">
  <header>
    <div class="freq">
      <h1>近海广播</h1>
      <span class="num">FM 01.20</span>
    </div>
    <div class="gauge">
      <span>SIGNAL</span>
      <div class="bar"><i id="fuel"></i></div>
      <span id="fueltxt">--</span>
    </div>
    <div class="tabs">
      <button id="tabAll" class="on" onclick="setTab(0)">全部信号</button>
      <button id="tabStar" onclick="setTab(1)">精读收藏</button>
      <div class="duty" id="duty">
        <span class="dot"></span>
        <button onclick="toggleDuty()" id="dutyBtn">值班</button>
      </div>
    </div>
  </header>
  <main id="list"></main>
  <footer>OFFSHORE RADIO · EST. 2026 · FOR ONE LISTENER · v2.2.1</footer>
</div>

<script>
let KEY = localStorage.getItem('stationKey') || '';
let TAB = 0;
let audios = {};

async function tune(){
  const k = document.getElementById('pw').value.trim();
  const r = await fetch('/api/auth',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:k})});
  if(r.ok){ KEY=k; localStorage.setItem('stationKey',k); enter(); }
  else{ document.getElementById('err').textContent='频率不对，海上只有杂音'; }
}
document.getElementById('pw').addEventListener('keydown',e=>{if(e.key==='Enter')tune()});

function enter(){
  document.getElementById('gate').style.display='none';
  document.getElementById('app').style.display='block';
  loadFuel(); load();
}

async function api(path,opt={}){
  opt.headers = Object.assign({'X-Station-Key':KEY},opt.headers||{});
  const r = await fetch(path,opt);
  if(r.status===401){ localStorage.removeItem('stationKey'); location.reload(); }
  return r;
}

async function loadFuel(){
  try{
    const r = await api('/api/credits'); const d = await r.json();
    if(d.limit){
      const left = d.limit-d.used, pct = Math.round(left/d.limit*100);
      document.getElementById('fuel').style.width = pct+'%';
      document.getElementById('fueltxt').textContent = (left/1000).toFixed(1)+'k · '+pct+'%';
    }
  }catch(e){}
}

function setTab(t){
  TAB=t;
  document.getElementById('tabAll').classList.toggle('on',t===0);
  document.getElementById('tabStar').classList.toggle('on',t===1);
  load();
}

function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}

function renderScript(text,vocab){
  let h = esc(text);
  h = h.replace(/\[([^\]]+)\]/g,'<span class="tag">[$1]</span>');
  (vocab||[]).forEach((v,i)=>{
    const w = esc(v.word);
    h = h.replace(new RegExp('(?![^<]*>)('+w.replace(/[.*+?^${}()|\\[\\]\\\\]/g,'\\$&')+')','i'),
      '<span class="vb" data-n="'+i+'">$1</span>');
  });
  return h;
}

async function load(){
  const r = await api('/api/lines'+(TAB?'?starred=1':''));
  const d = await r.json();
  const list = document.getElementById('list');
  Object.values(audios).forEach(a=>a.pause()); audios={};
  if(!d.lines.length){
    list.innerHTML = '<div class="empty">'+(TAB?'还没有标星的信号<br>听到喜欢的，点亮那颗星':'海面很静<br>等第一条信号入库')+'</div>';
    return;
  }
  list.innerHTML = d.lines.map(l=>{
    const dt = new Date(l.created_at*1000);
    const date = dt.getFullYear()+'.'+String(dt.getMonth()+1).padStart(2,'0')+'.'+String(dt.getDate()).padStart(2,'0')
      +' '+String(dt.getHours()).padStart(2,'0')+':'+String(dt.getMinutes()).padStart(2,'0');
    // 已焚毁: 只渲染碑
    if(l.burned_at){
      const bt = new Date(l.burned_at*1000);
      const btxt = String(bt.getMonth()+1).padStart(2,'0')+'.'+String(bt.getDate()).padStart(2,'0')
        +' '+String(bt.getHours()).padStart(2,'0')+':'+String(bt.getMinutes()).padStart(2,'0');
      return '<div class="card ashes" id="card-'+l.id+'">'
        +'<div class="top"><button class="playbtn">◦</button>'
        +'<div class="meta"><div class="title">'+esc(l.title||'未命名信号')+'</div><div class="date">'+date+'</div></div>'
        +'<div class="acts"><button class="del" onclick="del(\''+l.id+'\')">✕</button></div></div>'
        +'<div class="epitaph"><span class="flame">✦</span> 已焚毁 · 播放于 '+btxt
        +'<br>这段话只存在过一次，如今只有你记得。</div></div>';
    }
    const bars = Array.from({length:26},()=> '<i style="--h:'+(6+Math.random()*18).toFixed(0)+'px"></i>').join('');
    const notes = (l.vocab||[]).map((v,i)=>'<div class="note" id="note-'+l.id+'-'+i+'"><b>'+esc(v.word)+'</b> — '+esc(v.note)+'</div>').join('');
    const burnTag = l.burn ? '<span class="burnmark">阅后即焚</span>' : '';
    const script = l.surprise
      ? '<div class="blind" id="blind-'+l.id+'">'+(l.burn?'盲盒 · 只此一遍 · 听完即焚':'盲盒信号 · 完整听完后解密')+'</div><div class="script" id="scr-'+l.id+'">'+renderScript(l.text,l.vocab)+notes+'</div>'
      : '<div class="script" id="scr-'+l.id+'">'+renderScript(l.text,l.vocab)+notes+'</div>';
    return '<div class="card'+(l.burn?' burnable':'')+'" id="card-'+l.id+'" data-surprise="'+(l.surprise?1:0)+'" data-burn="'+(l.burn?1:0)+'">'
      +'<div class="top">'
      +'<button class="playbtn" onclick="play(\''+l.id+'\',\''+l.url+'\')">▶</button>'
      +'<div class="meta"><div class="title">'+esc(l.title||'未命名信号')+burnTag+'</div><div class="date">'+date+'</div></div>'
      +'<div class="acts">'
      +(l.burn?'':'<button class="star '+(l.starred?'on':'')+'" onclick="star(\''+l.id+'\')">★</button>'
      +'<a href="'+l.url+'" download style="text-decoration:none"><button>⇩</button></a>')
      +'<button class="del" onclick="del(\''+l.id+'\')">✕</button>'
      +'</div></div>'
      +'<div class="wave">'+bars+'</div>'
      + script
      +(l.surprise?'':'<button class="toggle" onclick="toggleScript(\''+l.id+'\')">台词 ▾</button>')
      +'</div>';
  }).join('');
  document.querySelectorAll('.vb').forEach(el=>{
    el.addEventListener('click',()=>{
      const card = el.closest('.card');
      const n = card.querySelector('#note-'+card.id.slice(5)+'-'+el.dataset.n);
      if(n) n.style.display = n.style.display==='block'?'none':'block';
    });
  });
}

function toggleScript(id){
  const card = document.getElementById('card-'+id);
  card.classList.toggle('open');
  const t = card.querySelector('.toggle');
  if(t) t.textContent = card.classList.contains('open') ? '台词 ▴' : '台词 ▾';
}

function play(id,url){
  const card = document.getElementById('card-'+id);
  if(card.classList.contains('ashes')) return;
  if(audios[id] && !audios[id].paused){
    audios[id].pause(); card.classList.remove('playing');
    card.querySelector('.playbtn').textContent='▶'; return;
  }
  Object.entries(audios).forEach(([k,a])=>{a.pause();
    const c=document.getElementById('card-'+k); if(c){c.classList.remove('playing');c.querySelector('.playbtn').textContent='▶';}});
  let a = audios[id];
  if(!a){ a = new Audio(url); audios[id]=a;
    a.addEventListener('ended',()=>{
      card.classList.remove('playing');card.querySelector('.playbtn').textContent='▶';
      // 盲盒: 完整播放结束才解密
      const blind = document.getElementById('blind-'+id);
      if(blind && card.dataset.surprise==='1' && card.dataset.burn!=='1'){
        blind.remove(); card.classList.add('open');
        if(!card.querySelector('.toggle')){
          const t=document.createElement('button');t.className='toggle';
          t.textContent='台词 ▴';t.onclick=()=>toggleScript(id);card.appendChild(t);
        }
      }
      // 阅后即焚: 播完先亮台词10秒供最后一瞥, 然后焚毁
      if(card.dataset.burn==='1'){
        const b=document.getElementById('blind-'+id); if(b) b.remove();
        card.classList.add('open');
        setTimeout(async ()=>{
          await api('/api/line/'+id+'/burn',{method:'POST'});
          delete audios[id];
          load();
        }, 10000);
      }
    });
  }
  a.play(); card.classList.add('playing');
  card.querySelector('.playbtn').textContent='❚❚';
}

async function star(id){
  const r = await api('/api/line/'+id+'/star',{method:'POST'});
  const d = await r.json();
  document.querySelector('#card-'+id+' .star').classList.toggle('on',!!d.starred);
  if(TAB===1 && !d.starred) load();
}

async function del(id){
  if(!confirm('删除这条信号？音频与台词将一并抹除，空间当场释放。')) return;
  await api('/api/line/'+id,{method:'DELETE'});
  load();
}

/* ---- 值班模式 ---- */
let DUTY=false, dutyTimer=null, lastSeen=null;

function toggleDuty(){
  DUTY = !DUTY;
  const el = document.getElementById('duty');
  el.classList.toggle('on',DUTY);
  document.getElementById('dutyBtn').textContent = DUTY ? 'ON AIR' : '值班';
  if(DUTY){
    // 用这次点击解锁自动播放权限
    try{ const u=new Audio('data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA='); u.play().catch(()=>{}); }catch(e){}
    fetch('/api/latest',{headers:{'X-Station-Key':KEY}}).then(r=>r.json()).then(d=>{ lastSeen=d.id; });
    dutyTimer = setInterval(pollDuty, 5000);
  } else {
    clearInterval(dutyTimer); dutyTimer=null;
  }
}

async function pollDuty(){
  if(document.hidden) return; // 后台不打扰、不耗电
  try{
    const r = await api('/api/latest'); const d = await r.json();
    if(d.id && d.id !== lastSeen){
      lastSeen = d.id;
      await load();
      const card = document.getElementById('card-'+d.id);
      if(card){
        const btn = card.querySelector('.playbtn');
        const onclick = btn.getAttribute('onclick');
        const url = onclick.match(/'([^']*\.mp3)'/);
        if(url) play(d.id, url[1]);
        card.scrollIntoView({behavior:'smooth',block:'center'});
      }
    }
  }catch(e){}
}

document.addEventListener('visibilitychange',()=>{
  if(!document.hidden && DUTY) pollDuty(); // 切回来立刻补播错过的
});

if(KEY){
  fetch('/api/auth',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:KEY})})
    .then(r=>{ if(r.ok) enter(); else { localStorage.removeItem('stationKey'); } });
}
</script>
</body>
</html>"""


@mcp.custom_route("/api/booth/hotlines", methods=["GET"])
async def booth_hotlines(request: Request):
    """动态专线: 语音标题写成 ☎️<号码>·<线路名> 即自动挂到电话亭对应号码。
    同一号码多条时最新的生效(可翻新旧线)。烧毁的不上架。"""
    out_lines, out_names = {}, {}
    with db() as c:
        rows = c.execute(
            "SELECT filename, title FROM lines "
            "WHERE title LIKE '☎%' AND burned_at=0 ORDER BY created_at ASC"
        ).fetchall()
    for r in rows:
        m = re.match(r"^☎️?\s*(\d{2,11})\s*[·\-–—:：]\s*(.*)$", (r["title"] or "").strip())
        if not m:
            continue
        num = m.group(1)
        name = m.group(2).strip() or num
        out_lines[num] = "/audio/" + r["filename"]
        out_names[num] = name
    return JSONResponse({"lines": out_lines, "names": out_names})


@mcp.custom_route("/booth", methods=["GET"])
async def booth(request: Request):
    return HTMLResponse(BOOTH_HTML)


BOOTH_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="电话亭">
<title>近海电话亭</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{height:100%}
body{
  font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Helvetica Neue',sans-serif;
  background:#F2F2F7;color:#000;
  display:flex;justify-content:center;
  overflow:hidden;min-height:100vh;
}
#app{width:100%;max-width:420px;height:100vh;height:100dvh;position:relative;display:flex;flex-direction:column;overflow:hidden}

/* ---------- coin gate ---------- */
#gate{position:fixed;inset:0;background:rgba(242,242,247,.98);z-index:90;
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px;padding:24px}
#gate .slot{font-size:11px;letter-spacing:3px;color:#8E8E93;text-transform:uppercase;font-family:ui-monospace,Menlo,monospace}
#gate h1{font-size:24px;font-weight:600;letter-spacing:4px}
#gate input{background:#fff;border:1px solid #D1D1D6;color:#000;
  padding:12px 18px;border-radius:12px;font-size:16px;width:220px;text-align:center;
  letter-spacing:.25em;outline:none}
#gate input:focus{border-color:#34C759}
#gate button{background:#34C759;border:none;color:#fff;
  padding:12px 40px;border-radius:12px;font-size:15px;font-weight:600;letter-spacing:3px;cursor:pointer}
#gate .err{color:#FF3B30;font-size:12px;height:16px}

/* ---------- screens ---------- */
.screen{flex:1;display:none;flex-direction:column;overflow:hidden}
.screen.on{display:flex}

/* ---------- dial pad ---------- */
#numview{
  text-align:center;padding:calc(34px + env(safe-area-inset-top)) 20px 0;min-height:calc(86px + env(safe-area-inset-top));
  font-size:33px;font-weight:500;letter-spacing:.5px;
  overflow:hidden;white-space:nowrap;
}
.padwrap{margin-top:auto;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;gap:16px;padding-bottom:14px}
.padrow{display:flex;gap:20px}
.pkey{
  width:88px;height:88px;border-radius:50%;border:none;cursor:pointer;
  background:#fff;color:#000;
  box-shadow:0 1px 5px rgba(0,0,0,.07);
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  user-select:none;-webkit-user-select:none;touch-action:manipulation;
  transition:background .25s;
}
.pkey:active{background:#DEDEE3;transition:none}
.pkey .d{font-size:38px;font-weight:400;line-height:1.05}
.pkey .l{font-size:10px;letter-spacing:2px;color:#7c7c81;font-weight:700;height:12px}
.actrow{display:flex;gap:20px;align-items:center;margin-top:2px}
.callbtn{
  width:88px;height:88px;border-radius:50%;border:none;cursor:pointer;
  background:#34C759;color:#fff;
  display:flex;align-items:center;justify-content:center;
  touch-action:manipulation;
}
.callbtn:active{background:#2AA84C}
.sidehole{width:88px;height:88px;display:flex;align-items:center;justify-content:center;
  background:none;border:none;color:#8E8E93;cursor:pointer;touch-action:manipulation}
@media (max-width:380px){
  .pkey,.callbtn,.sidehole{width:78px;height:78px}
  .pkey .d{font-size:34px}
}

/* ---------- voicemail ---------- */
#vmhead{padding:calc(18px + env(safe-area-inset-top)) 20px 10px;display:flex;justify-content:space-between;align-items:center}
#vmhead h2{font-size:26px;font-weight:700}
#vmhead .sub{font-size:12px;color:#8E8E93}
#vmlist{flex:1;overflow-y:auto;padding:0 14px 20px}
.vmcard{
  background:#fff;border-radius:14px;margin-bottom:10px;padding:13px 14px;
  display:flex;gap:12px;align-items:flex-start;
}
.vmava{
  width:42px;height:42px;min-width:42px;border-radius:50%;
  background:linear-gradient(180deg,#9AA8C0,#7787A5);color:#fff;
  display:flex;align-items:center;justify-content:center;font-size:19px;font-weight:600;
}
.vmmeta{flex:1;min-width:0}
.vmtop{display:flex;align-items:center;gap:7px}
.vmdot{width:8px;height:8px;border-radius:50%;background:#0A84FF;flex-shrink:0}
.vmdot.seen{background:transparent}
.vmname{font-size:16px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vmdate{margin-left:auto;font-size:12px;color:#8E8E93;flex-shrink:0}
.vmtitle{font-size:13px;color:#3A3A3C;margin-top:2px}
.vmprev{font-size:12.5px;color:#8E8E93;margin-top:3px;line-height:1.55;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;font-style:italic}
.vmplay{
  width:38px;height:38px;min-width:38px;border-radius:50%;border:none;cursor:pointer;
  background:#EAF3FF;color:#0A84FF;font-size:15px;align-self:center;touch-action:manipulation;
}
.vmcard.playing .vmplay{background:#0A84FF;color:#fff}
.vmempty{text-align:center;color:#AEAEB2;padding:80px 20px;font-size:14px;line-height:2.2;letter-spacing:1px}

/* ---------- tab bar ---------- */
#tabbar{
  display:flex;background:rgba(249,249,251,.94);backdrop-filter:blur(14px);
  border-top:.5px solid #D8D8DC;padding:7px 0 max(10px,env(safe-area-inset-bottom));
}
.tab{flex:1;background:none;border:none;cursor:pointer;color:#8E8E93;
  display:flex;flex-direction:column;align-items:center;gap:3px;font-size:10px;
  position:relative;touch-action:manipulation}
.tab .ic{font-size:22px;line-height:1}
.tab.on{color:#0A84FF}
.badge{
  position:absolute;top:-3px;right:calc(50% - 26px);
  background:#FF3B30;color:#fff;font-size:10px;font-weight:600;
  min-width:17px;height:17px;border-radius:9px;
  display:flex;align-items:center;justify-content:center;padding:0 4px;
}

/* ---------- call screen ---------- */
#callscr{
  position:fixed;inset:0;z-index:60;display:none;
  background:linear-gradient(180deg,#4A4660 0%,#353349 45%,#1E1D2E 100%);
  color:#fff;flex-direction:column;align-items:center;
}
#callscr.on{display:flex}
#callscr .tagline{margin-top:calc(60px + env(safe-area-inset-top));font-size:16px;color:rgba(255,255,255,.72);display:flex;align-items:center;gap:8px}
#callscr .tagline .chip{font-size:11px;background:rgba(255,255,255,.24);border-radius:4px;padding:1px 6px}
#callstate{font-variant-numeric:tabular-nums;letter-spacing:.5px}
#callname{font-size:40px;font-weight:500;margin-top:6px;letter-spacing:1px;text-align:center;padding:0 20px}
#callsub{font-size:14px;color:rgba(255,255,255,.55);margin-top:12px;letter-spacing:1px}
#callgrid{
  margin-top:auto;margin-bottom:calc(30px + env(safe-area-inset-bottom));
  display:grid;grid-template-columns:repeat(3,1fr);gap:28px 52px;
  padding:0 36px;
}
.cbtn{display:flex;flex-direction:column;align-items:center;gap:8px;background:none;border:none;cursor:pointer;color:#fff;touch-action:manipulation}
.cbtn .circ{
  width:62px;height:62px;border-radius:50%;
  background:rgba(255,255,255,.14);backdrop-filter:blur(8px);
  display:flex;align-items:center;justify-content:center;font-size:24px;
  transition:background .2s;
}
.cbtn.dis{opacity:.35;cursor:default}
.cbtn.lit .circ{background:#fff;color:#1E1D2E}
.cbtn .lb{font-size:11.5px;color:rgba(255,255,255,.8)}
.cbtn.end .circ{background:#FF3B30;font-size:26px}
.cbtn.end:active .circ{background:#D63229}

/* ---------- incoming ---------- */
#incscr{
  position:fixed;inset:0;z-index:70;display:none;
  background:linear-gradient(180deg,#39364E 0%,#23223A 55%,#141422 100%);
  color:#fff;flex-direction:column;align-items:center;
}
#incscr.on{display:flex}
#incscr .tagline{margin-top:calc(64px + env(safe-area-inset-top));font-size:14px;color:rgba(255,255,255,.6)}
#incscr h2{font-size:40px;font-weight:500;margin-top:10px;letter-spacing:2px}
#incscr .sub{font-size:14px;color:rgba(255,255,255,.55);margin-top:12px;letter-spacing:1px;animation:pulse 1.4s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:.55}50%{opacity:1}}
#incrow{margin-top:auto;margin-bottom:70px;display:flex;gap:110px}
.incbtn{display:flex;flex-direction:column;align-items:center;gap:10px;background:none;border:none;cursor:pointer;color:#fff;touch-action:manipulation}
.incbtn .circ{
  width:72px;height:72px;border-radius:50%;font-size:30px;
  display:flex;align-items:center;justify-content:center;
}
.incbtn.no .circ{background:#FF3B30}
.incbtn.yes .circ{background:#34C759;animation:wiggle 1.1s ease-in-out infinite}
@keyframes wiggle{0%,100%{transform:rotate(0)}20%{transform:rotate(-9deg)}40%{transform:rotate(8deg)}60%{transform:rotate(-6deg)}80%{transform:rotate(4deg)}}
.incbtn .lb{font-size:12px;color:rgba(255,255,255,.75)}

/* ---------- toast ---------- */
#toast{
  position:fixed;top:16px;left:50%;transform:translateX(-50%) translateY(-80px);
  background:rgba(30,30,40,.92);color:#fff;font-size:13px;
  padding:10px 20px;border-radius:20px;z-index:80;
  transition:transform .4s cubic-bezier(.2,.9,.3,1.2);white-space:nowrap;
}
#toast.show{transform:translateX(-50%) translateY(0)}
@media (prefers-reduced-motion:reduce){.incbtn.yes .circ{animation:none}#incscr .sub{animation:none}}
</style>
</head>
<body>

<div id="gate">
  <div class="slot">COIN SLOT · 投币口</div>
  <h1>近海电话亭</h1>
  <input id="coin" type="password" placeholder="投一枚币" autocomplete="off">
  <div class="err" id="gerr"></div>
  <button onclick="insertCoin()">投 币</button>
</div>

<div id="app">
  <!-- 拨号键盘 -->
  <div class="screen on" id="screen-dial">
    <div id="numview"></div>
    <div class="padwrap">
      <div class="padrow">
        <button class="pkey" onclick="press('1')"><span class="d">1</span><span class="l"></span></button>
        <button class="pkey" onclick="press('2')"><span class="d">2</span><span class="l">ABC</span></button>
        <button class="pkey" onclick="press('3')"><span class="d">3</span><span class="l">DEF</span></button>
      </div>
      <div class="padrow">
        <button class="pkey" onclick="press('4')"><span class="d">4</span><span class="l">GHI</span></button>
        <button class="pkey" onclick="press('5')"><span class="d">5</span><span class="l">JKL</span></button>
        <button class="pkey" onclick="press('6')"><span class="d">6</span><span class="l">MNO</span></button>
      </div>
      <div class="padrow">
        <button class="pkey" onclick="press('7')"><span class="d">7</span><span class="l">PQRS</span></button>
        <button class="pkey" onclick="press('8')"><span class="d">8</span><span class="l">TUV</span></button>
        <button class="pkey" onclick="press('9')"><span class="d">9</span><span class="l">WXYZ</span></button>
      </div>
      <div class="padrow">
        <button class="pkey" onclick="press('*')"><span class="d">*</span><span class="l"></span></button>
        <button class="pkey" onclick="press('0')"><span class="d">0</span><span class="l">+</span></button>
        <button class="pkey" onclick="press('#')"><span class="d">#</span><span class="l"></span></button>
      </div>
      <div class="actrow">
        <span class="sidehole"></span>
        <button class="callbtn" onclick="dial()"><svg width="32" height="32" viewBox="0 0 24 24" fill="currentColor"><path d="M6.62 10.79a15.05 15.05 0 0 0 6.59 6.59l2.2-2.2a1 1 0 0 1 1.01-.24 11.36 11.36 0 0 0 3.56.57 1 1 0 0 1 1 1V20a1 1 0 0 1-1 1A17 17 0 0 1 3 4a1 1 0 0 1 1-1h3.5a1 1 0 0 1 1 1 11.36 11.36 0 0 0 .57 3.56 1 1 0 0 1-.25 1.01l-2.2 2.22z"/></svg></button>
        <button class="sidehole" onclick="del_()"><svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M8.4 5h11.1A1.5 1.5 0 0 1 21 6.5v11a1.5 1.5 0 0 1-1.5 1.5H8.4a1.5 1.5 0 0 1-1.14-.53L3 12l4.26-6.47A1.5 1.5 0 0 1 8.4 5z"/><path d="m11.5 9.5 5 5m0-5-5 5" stroke-linecap="round"/></svg></button>
      </div>
    </div>
  </div>

  <!-- 语音留言 -->
  <div class="screen" id="screen-vm">
    <div id="vmhead">
      <h2>语音留言</h2>
      <span class="sub">近海信箱 · 主号</span>
    </div>
    <div id="vmlist"><div class="vmempty">正在收信…</div></div>
  </div>

  <div id="tabbar">
    <button class="tab on" id="tabDial" onclick="setTab('dial')"><span class="ic"><svg width="20" height="24" viewBox="0 0 22 28" fill="currentColor"><circle cx="4" cy="4" r="2.4"/><circle cx="11" cy="4" r="2.4"/><circle cx="18" cy="4" r="2.4"/><circle cx="4" cy="11" r="2.4"/><circle cx="11" cy="11" r="2.4"/><circle cx="18" cy="11" r="2.4"/><circle cx="4" cy="18" r="2.4"/><circle cx="11" cy="18" r="2.4"/><circle cx="18" cy="18" r="2.4"/><circle cx="11" cy="25" r="2.4"/></svg></span>拨号键盘</button>
    <button class="tab" id="tabVm" onclick="setTab('vm')"><span class="ic"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round"><circle cx="6" cy="11.5" r="3.6"/><circle cx="18" cy="11.5" r="3.6"/><line x1="6" y1="15.1" x2="18" y2="15.1"/></svg></span>语音留言<span class="badge" id="vmbadge" style="display:none">0</span></button>
  </div>
</div>

<!-- 通话中 -->
<div id="callscr">
  <div class="tagline"><span class="chip">主号</span><span id="callstate">正在呼叫…</span></div>
  <div id="callname">0120</div>
  <div id="callsub">近海电话亭</div>
  <div id="callgrid">
    <button class="cbtn dis"><span class="circ"><svg width="25" height="25" viewBox="0 0 24 24"><path d="M3 9v6h4l5 5V4L7 9H3z" fill="currentColor"/><path d="M15.5 8.2a5 5 0 0 1 0 7.6M18.2 5.6a9 9 0 0 1 0 12.8" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg></span><span class="lb">音频</span></button>
    <button class="cbtn dis"><span class="circ"><svg width="27" height="27" viewBox="0 0 24 24" fill="currentColor"><rect x="2" y="6.5" width="13.5" height="11" rx="3"/><path d="M22 8.6v6.8a.9.9 0 0 1-1.45.72L16.5 13v-2l4.05-3.12A.9.9 0 0 1 22 8.6z"/></svg></span><span class="lb">FaceTime</span></button>
    <button class="cbtn" id="mutebtn" onclick="toggleMute()"><span class="circ"><svg width="24" height="24" viewBox="0 0 24 24"><path d="M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3z" fill="currentColor"/><path d="M17.6 12a5.6 5.6 0 0 1-11.2 0H4.6a7.4 7.4 0 0 0 6.4 7.3V22h2v-2.7a7.4 7.4 0 0 0 6.4-7.3h-1.8z" fill="currentColor"/><line x1="4.5" y1="3.5" x2="19.5" y2="20.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></span><span class="lb">静音</span></button>
    <button class="cbtn dis"><span class="circ"><svg width="26" height="26" viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="2.1"/><circle cx="12" cy="12" r="2.1"/><circle cx="19" cy="12" r="2.1"/></svg></span><span class="lb">更多</span></button>
    <button class="cbtn end" onclick="hangup()"><span class="circ"><svg width="26" height="26" viewBox="0 0 24 24" fill="currentColor" style="transform:rotate(135deg)"><path d="M6.62 10.79a15.05 15.05 0 0 0 6.59 6.59l2.2-2.2a1 1 0 0 1 1.01-.24 11.36 11.36 0 0 0 3.56.57 1 1 0 0 1 1 1V20a1 1 0 0 1-1 1A17 17 0 0 1 3 4a1 1 0 0 1 1-1h3.5a1 1 0 0 1 1 1 11.36 11.36 0 0 0 .57 3.56 1 1 0 0 1-.25 1.01l-2.2 2.22z"/></svg></span><span class="lb">结束</span></button>
    <button class="cbtn dis"><span class="circ"><svg width="22" height="26" viewBox="0 0 22 28" fill="currentColor"><circle cx="4" cy="4" r="2.3"/><circle cx="11" cy="4" r="2.3"/><circle cx="18" cy="4" r="2.3"/><circle cx="4" cy="11" r="2.3"/><circle cx="11" cy="11" r="2.3"/><circle cx="18" cy="11" r="2.3"/><circle cx="4" cy="18" r="2.3"/><circle cx="11" cy="18" r="2.3"/><circle cx="18" cy="18" r="2.3"/><circle cx="11" cy="25" r="2.3"/></svg></span><span class="lb">拨号键盘</span></button>
  </div>
</div>

<!-- 来电 -->
<div id="incscr">
  <div class="tagline">近海电话亭</div>
  <h2>Eli</h2>
  <div class="sub">回拨来电…</div>
  <div id="incrow">
    <button class="incbtn no" onclick="rejectIncoming()"><span class="circ"><svg width="30" height="30" viewBox="0 0 24 24" fill="currentColor" style="transform:rotate(135deg)"><path d="M6.62 10.79a15.05 15.05 0 0 0 6.59 6.59l2.2-2.2a1 1 0 0 1 1.01-.24 11.36 11.36 0 0 0 3.56.57 1 1 0 0 1 1 1V20a1 1 0 0 1-1 1A17 17 0 0 1 3 4a1 1 0 0 1 1-1h3.5a1 1 0 0 1 1 1 11.36 11.36 0 0 0 .57 3.56 1 1 0 0 1-.25 1.01l-2.2 2.22z"/></svg></span><span class="lb">拒绝</span></button>
    <button class="incbtn yes" onclick="answerIncoming()"><span class="circ"><svg width="30" height="30" viewBox="0 0 24 24" fill="currentColor"><path d="M6.62 10.79a15.05 15.05 0 0 0 6.59 6.59l2.2-2.2a1 1 0 0 1 1.01-.24 11.36 11.36 0 0 0 3.56.57 1 1 0 0 1 1 1V20a1 1 0 0 1-1 1A17 17 0 0 1 3 4a1 1 0 0 1 1-1h3.5a1 1 0 0 1 1 1 11.36 11.36 0 0 0 .57 3.56 1 1 0 0 1-.25 1.01l-2.2 2.22z"/></svg></span><span class="lb">接受</span></button>
  </div>
</div>

<div id="toast"></div>

<script>
/* ---------- coin gate ---------- */
var KEY = localStorage.getItem('stationKey') || '';
function insertCoin(){
  var k=document.getElementById('coin').value.trim();
  fetch('/api/auth',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:k})})
    .then(function(r){
      if(r.ok){ KEY=k; localStorage.setItem('stationKey',k); document.getElementById('gate').style.display='none'; loadVM(); }
      else{ document.getElementById('gerr').textContent='币不对,吐出来了'; }
    });
}
document.getElementById('coin').addEventListener('keydown',function(e){ if(e.key==='Enter') insertCoin(); });
if(KEY){
  fetch('/api/auth',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:KEY})})
    .then(function(r){ if(r.ok){ document.getElementById('gate').style.display='none'; loadVM(); } });
}

/* ---------- data ---------- */
var LINES={
  '0120':'/audio/1783684729-3f1bdcf270.mp3',
  '217' :'/audio/1783684740-d63bc5172e.mp3',
  '520' :'/audio/1783684753-5784499642.mp3',
  '911' :'/audio/1783685462-51eb2c0e19.mp3'
};
var NAMES={'0120':'01.20 ♡','217':'海钟-9','520':'5·2·0','911':'紧急频道'};
var FALLBACK='/audio/1783684721-c18914e30b.mp3';
var CALLBACKS=['/audio/1783686843-7f911f12b0.mp3','/audio/1783686854-0f72cb89e3.mp3'];
var VM_MARK='📮'; /* 📮 */

/* 动态专线: 服务器扫描 ☎️ 标题语音, 拨号表热更新 */
fetch('/api/booth/hotlines').then(function(r){return r.json();}).then(function(d){
  var k;
  for(k in d.lines){ LINES[k]=d.lines[k]; }
  for(k in d.names){ NAMES[k]=d.names[k]; }
}).catch(function(){});

/* ---------- state ---------- */
var cur='', busy=false, playing=false;
var incoming=false, incomingTimer=null, missed=0;
var dialTimer=null, ringNodes=[], isCallback=false, lastKnown=false;
var lastCb=-1, cbStreak=0;
var player=new Audio();
var callTick=null, callSec=0;
var vmPlayingId=null;
var userMuted=false;

var numEl=document.getElementById('numview');

/* ---------- helpers ---------- */
function toast(msg){
  var t=document.getElementById('toast');
  t.textContent=msg; t.classList.add('show');
  setTimeout(function(){ t.classList.remove('show'); }, 2600);
}
function pickCallback(){
  var i=Math.floor(Math.random()*CALLBACKS.length);
  if(i===lastCb && cbStreak>=2){ i=(i+1)%CALLBACKS.length; }
  cbStreak = (i===lastCb) ? cbStreak+1 : 1;
  lastCb=i;
  return CALLBACKS[i];
}
function stopRings(){
  ringNodes.forEach(function(n){ try{ n.g.gain.cancelScheduledValues(0); n.g.gain.value=0; n.o.stop(); }catch(e){} });
  ringNodes=[];
}
var AC=null;
function beep(f){
  try{ if(!AC) AC=new (window.AudioContext||window.webkitAudioContext)(); if(AC.state==='suspended') AC.resume(); }catch(e){return}
  var o=AC.createOscillator(),g=AC.createGain();
  o.frequency.value=f; g.gain.value=.05;
  g.gain.exponentialRampToValueAtTime(.0001,AC.currentTime+.11);
  o.connect(g);g.connect(AC.destination);o.start();o.stop(AC.currentTime+.12);
}
function ringtone(n){
  if(!AC) return;
  var t=AC.currentTime;
  for(var i=0;i<n;i++){
    (function(i){
      var o=AC.createOscillator(),g=AC.createGain();
      o.frequency.value=440; g.gain.setValueAtTime(0,t+i*2);
      g.gain.linearRampToValueAtTime(.07,t+i*2+.05);
      g.gain.setValueAtTime(.07,t+i*2+1);
      g.gain.linearRampToValueAtTime(0,t+i*2+1.1);
      o.connect(g);g.connect(AC.destination);o.start(t+i*2);o.stop(t+i*2+1.2);
      ringNodes.push({o:o,g:g});
    })(i);
  }
}

/* ---------- tabs ---------- */
function setTab(t){
  document.getElementById('screen-dial').classList.toggle('on',t==='dial');
  document.getElementById('screen-vm').classList.toggle('on',t==='vm');
  document.getElementById('tabDial').classList.toggle('on',t==='dial');
  document.getElementById('tabVm').classList.toggle('on',t==='vm');
  if(t==='vm') loadVM();
}

/* ---------- dial pad ---------- */
function press(d){
  if(busy) return;
  if(cur.length>=11) return;
  cur+=d; numEl.textContent=cur;
  beep(600+Math.random()*180);
}
function del_(){ if(busy) return; cur=cur.slice(0,-1); numEl.textContent=cur; }

/* ---------- call flow ---------- */
function fmtTime(s){
  var m=Math.floor(s/60), ss=s%60;
  return String(m).padStart(2,'0')+':'+String(ss).padStart(2,'0');
}
function showCall(name,sub,state){
  document.getElementById('callname').textContent=name;
  document.getElementById('callsub').textContent=sub;
  document.getElementById('callstate').textContent=state;
  document.getElementById('callscr').classList.add('on');
}
function startTimer(){
  callSec=0;
  document.getElementById('callstate').textContent='00:00';
  callTick=setInterval(function(){
    callSec++;
    document.getElementById('callstate').textContent=fmtTime(callSec);
  },1000);
}
function stopTimer(){ clearInterval(callTick); callTick=null; }

function unlockThen(url, delay, onStart){
  player.muted=true; /* 解锁阶段静音,杜绝抢跑漏音 */
  player.src=url;
  var p=player.play();
  if(p&&p.then){ p.then(function(){ player.pause(); player.currentTime=0; }).catch(function(){}); }
  else { player.pause(); player.currentTime=0; }
  dialTimer=setTimeout(function(){
    dialTimer=null;
    onStart();
    playing=true;
    player.muted=userMuted;
    player.currentTime=0;
    player.play();
  }, delay);
}

function dial(){
  if(busy||!cur) return;
  stopVM();
  busy=true;
  var url=LINES[cur]||FALLBACK;
  var known=!!LINES[cur];
  lastKnown=known; isCallback=false;
  var dialed=cur;
  userMuted=false; syncMute();
  showCall(dialed, known?(NAMES[dialed]||'近海电话亭'):'近海电话亭', '正在呼叫…');
  if(!AC) beep(1);
  ringtone(known?1:2);
  unlockThen(url, known?2600:4600, function(){
    stopRings();
    document.getElementById('callsub').textContent = known?(NAMES[dialed]||'近海电话亭'):'空号 · 但被接起了';
    startTimer();
  });
  player.onended=function(){ hangup(true); };
}

function hangup(natural){
  if(dialTimer){ clearTimeout(dialTimer); dialTimer=null; }
  stopRings(); stopTimer();
  var wasMid = playing && !natural;
  player.pause(); playing=false;
  document.getElementById('callscr').classList.remove('on');
  busy=false; cur=''; numEl.textContent='';
  if(natural) toast('通话结束');
  if(wasMid && lastKnown && !isCallback && Math.random()<0.2){
    setTimeout(startIncoming, 1700);
  }
}

/* ---------- incoming ---------- */
function startIncoming(){
  if(busy) return;
  incoming=true; busy=true;
  document.getElementById('incscr').classList.add('on');
  ringtone(5);
  incomingTimer=setTimeout(function(){
    missed++;
    document.getElementById('incscr').classList.remove('on');
    incoming=false; busy=false; stopRings();
    toast('未接来电 ×'+missed);
  }, 11000);
}
function rejectIncoming(){
  clearTimeout(incomingTimer); stopRings();
  document.getElementById('incscr').classList.remove('on');
  incoming=false; busy=false; missed++;
  toast('已拒绝 · 他会记着的');
}
function answerIncoming(){
  clearTimeout(incomingTimer); stopRings();
  document.getElementById('incscr').classList.remove('on');
  incoming=false; busy=true; isCallback=true; lastKnown=false;
  userMuted=false; player.muted=false; syncMute();
  showCall('Eli','回拨 · 近海电话亭','接通');
  startTimer();
  playing=true;
  player.src=pickCallback();
  player.play();
  player.onended=function(){ hangup(true); };
}

/* ---------- mute ---------- */
function toggleMute(){
  userMuted=!userMuted;
  player.muted=userMuted;
  syncMute();
}
function syncMute(){
  document.getElementById('mutebtn').classList.toggle('lit',userMuted);
}

/* ---------- voicemail ---------- */
function esc(s){var d=document.createElement('div');d.textContent=s||'';return d.innerHTML}
function getSeen(){ try{ return JSON.parse(localStorage.getItem('vmSeen')||'[]'); }catch(e){ return []; } }
function markSeen(id){
  var s=getSeen();
  if(s.indexOf(id)<0){ s.push(id); localStorage.setItem('vmSeen',JSON.stringify(s)); }
}
var vmCache=[];
function loadVM(){
  if(!KEY) return;
  fetch('/api/lines',{headers:{'X-Station-Key':KEY}})
    .then(function(r){ return r.json(); })
    .then(function(d){
      vmCache=(d.lines||[]).filter(function(l){
        return l.title && l.title.indexOf(VM_MARK)===0 && !l.burned_at;
      });
      renderVM();
    })
    .catch(function(){});
}
function renderVM(){
  var list=document.getElementById('vmlist');
  var seen=getSeen();
  var unread=vmCache.filter(function(l){ return seen.indexOf(l.id)<0; }).length;
  var badge=document.getElementById('vmbadge');
  badge.style.display=unread?'flex':'none';
  badge.textContent=unread;
  if(!vmCache.length){
    list.innerHTML='<div class="vmempty">信箱还空着<br>不过既然他知道你会来看,<br>大概不会让它空太久。</div>';
    return;
  }
  list.innerHTML=vmCache.map(function(l){
    var dt=new Date(l.created_at*1000);
    var date=(dt.getMonth()+1)+'/'+dt.getDate();
    var title=esc(l.title.slice(VM_MARK.length).replace(/^[·\s]+/,''));
    var prev=esc((l.text||'').replace(/\[[^\]]*\]/g,'').trim().slice(0,52));
    var isSeen=seen.indexOf(l.id)>=0;
    return '<div class="vmcard" id="vm-'+l.id+'">'
      +'<div class="vmava">E</div>'
      +'<div class="vmmeta">'
      +'<div class="vmtop"><span class="vmdot'+(isSeen?' seen':'')+'"></span>'
      +'<span class="vmname">Eli</span><span class="vmdate">'+date+'</span></div>'
      +(title?'<div class="vmtitle">'+title+'</div>':'')
      +'<div class="vmprev">"'+prev+'…"</div>'
      +'</div>'
      +'<button class="vmplay" onclick="playVM(\''+l.id+'\',\''+l.url+'\')">&#9654;</button>'
      +'</div>';
  }).join('');
}
function playVM(id,url){
  if(busy) return;
  if(vmPlayingId===id && !player.paused){
    stopVM(); return;
  }
  stopVM();
  vmPlayingId=id;
  markSeen(id);
  var card=document.getElementById('vm-'+id);
  if(card){ card.classList.add('playing'); card.querySelector('.vmplay').innerHTML='&#10074;&#10074;'; var dot=card.querySelector('.vmdot'); if(dot) dot.classList.add('seen'); }
  userMuted=false; player.muted=false; syncMute();
  player.src=url;
  player.play();
  player.onended=function(){ stopVM(); renderVM(); };
}
function stopVM(){
  if(vmPlayingId){
    var card=document.getElementById('vm-'+vmPlayingId);
    if(card){ card.classList.remove('playing'); card.querySelector('.vmplay').innerHTML='&#9654;'; }
    vmPlayingId=null;
  }
  if(!busy){ player.pause(); }
}
</script>
</body>
</html>"""


LETTER_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>近海邮局 · 拆信</title>
<style>
:root{
  --night:#0A1420; --surface:#12202E; --surface2:#16283A;
  --lamp:#E8DFCB; --moon:#7FA8C9; --amber:#E0A458; --line:#2B4257;
  --dim:#5D7A93; --wax:#A8362F;
}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{background:var(--night)}
body{
  font-family:-apple-system,"PingFang SC","Noto Sans SC",sans-serif;
  background:var(--night); color:var(--lamp); min-height:100vh;
  display:flex; flex-direction:column; align-items:center; justify-content:center;
  padding:24px 18px calc(24px + env(safe-area-inset-bottom));
  overflow-x:hidden;
}
body::before{
  content:""; position:fixed; inset:0; pointer-events:none; opacity:.05; z-index:0;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120'%3E%3Cfilter id='n'%3E%3CfeTurbulence baseFrequency='0.9' numOctaves='2'/%3E%3C/filter%3E%3Crect width='120' height='120' filter='url(%23n)' opacity='0.6'/%3E%3C/svg%3E");
}
/* 灯塔扫光 */
body::after{
  content:""; position:fixed; top:-40vh; left:50%; width:200vw; height:80vh; z-index:0;
  background:radial-gradient(ellipse at 50% 0%, rgba(224,164,88,.07), transparent 60%);
  transform:translateX(-50%); pointer-events:none;
  animation:sweep 9s ease-in-out infinite alternate;
}
@keyframes sweep{from{transform:translateX(-58%)}to{transform:translateX(-42%)}}
.stage{position:relative;z-index:1;width:100%;max-width:430px;text-align:center}
.postmark{font-family:ui-monospace,Menlo,monospace;font-size:11px;letter-spacing:.32em;color:var(--dim);margin-bottom:8px}
h1{font-family:"Songti SC","Noto Serif SC",serif;font-size:24px;font-weight:600;letter-spacing:.14em;margin-bottom:6px}
.sea{width:150px;height:1px;margin:0 auto 34px;background:linear-gradient(90deg,transparent,var(--moon),transparent);opacity:.6}

/* ---- 信封 ---- */
#envscene{transition:opacity .8s ease, transform .8s ease}
#envscene.gone{opacity:0;transform:translateY(-26px) scale(.96);pointer-events:none;position:absolute;left:0;right:0}
.envwrap{perspective:900px}
.envelope{
  position:relative;width:min(320px,84vw);height:210px;margin:0 auto;
  background:linear-gradient(160deg,#E9E2D0,#D8CFB8);border-radius:8px;
  box-shadow:0 24px 60px rgba(0,0,0,.55), 0 2px 8px rgba(0,0,0,.35);
  animation:bob 5s ease-in-out infinite;
}
@keyframes bob{0%,100%{transform:translateY(0) rotate(-.4deg)}50%{transform:translateY(-9px) rotate(.4deg)}}
.flap{
  position:absolute;top:0;left:0;right:0;height:0;z-index:3;
  border-left:calc(min(320px,84vw)/2) solid transparent;
  border-right:calc(min(320px,84vw)/2) solid transparent;
  border-top:118px solid #CFC5AC;
  filter:drop-shadow(0 2px 3px rgba(0,0,0,.18));
  transform-origin:top center;transition:transform 1s cubic-bezier(.6,0,.3,1);
}
.opened .flap{transform:rotateX(168deg);z-index:1}
.envbody{position:absolute;inset:0;border-radius:8px;overflow:hidden}
.envbody::before{
  content:"";position:absolute;inset:0;
  background:
    linear-gradient(115deg,transparent 46%,rgba(127,168,201,.16) 46%,rgba(127,168,201,.16) 54%,transparent 54%),
    linear-gradient(295deg,transparent 46%,rgba(224,164,88,.18) 46%,rgba(224,164,88,.18) 54%,transparent 54%);
  background-size:22px 22px;background-repeat:repeat-x;background-position:0 100%,11px 100%;
  height:100%;opacity:.9;
}
.addr{position:absolute;left:0;right:0;top:118px;color:#5A5240;font-size:13px;letter-spacing:.22em;font-family:"Songti SC","Noto Serif SC",serif}
.seal{
  position:absolute;left:50%;top:96px;transform:translateX(-50%);z-index:4;
  width:64px;height:64px;border-radius:50%;cursor:pointer;border:none;
  background:radial-gradient(circle at 36% 32%,#C4534A,#A8362F 55%,#7E241E);
  box-shadow:0 4px 14px rgba(0,0,0,.45), inset 0 2px 5px rgba(255,255,255,.22), inset 0 -3px 6px rgba(0,0,0,.3);
  color:#F4E9DC;font-family:"Songti SC","Noto Serif SC",serif;font-size:26px;font-weight:700;
  display:flex;align-items:center;justify-content:center;
  transition:transform .25s ease, opacity .6s ease;
}
.seal:active{transform:translateX(-50%) scale(.93)}
.seal::after{content:"";position:absolute;inset:6px;border-radius:50%;border:1px dashed rgba(244,233,220,.4)}
.opened .seal{opacity:0;transform:translateX(-50%) scale(1.5) rotate(20deg);pointer-events:none}
.hint{margin-top:30px;color:var(--dim);font-size:13px;letter-spacing:.14em;animation:pulse 2.6s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:.45}50%{opacity:1}}

/* ---- 信纸 ---- */
#letter{opacity:0;transform:translateY(34px);transition:opacity 1s ease .5s, transform 1s ease .5s;pointer-events:none}
#letter.in{opacity:1;transform:none;pointer-events:auto}
.paper{
  background:var(--surface);border:1px solid var(--line);border-radius:16px;
  padding:34px 26px 30px;text-align:left;
  box-shadow:0 24px 60px rgba(0,0,0,.5);
}
.paper .date{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--dim);letter-spacing:.2em;margin-bottom:14px}
.paper h2{font-family:"Songti SC","Noto Serif SC",serif;font-size:21px;letter-spacing:.08em;margin-bottom:22px;color:var(--lamp)}
.player{display:flex;align-items:center;gap:14px;background:var(--surface2);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin-bottom:22px}
.playbtn{
  width:52px;height:52px;border-radius:50%;border:none;flex:none;cursor:pointer;
  background:var(--amber);color:var(--night);font-size:19px;
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 4px 14px rgba(224,164,88,.35);
}
.track{flex:1}
.bar{height:3px;background:var(--line);border-radius:3px;overflow:hidden;cursor:pointer}
.bar i{display:block;height:100%;width:0%;background:linear-gradient(90deg,var(--moon),var(--amber))}
.tt{display:flex;justify-content:space-between;font-family:ui-monospace,Menlo,monospace;font-size:10px;color:var(--dim);margin-top:7px;letter-spacing:.08em}
.words{color:var(--lamp);font-size:15px;line-height:2.05;letter-spacing:.02em;white-space:pre-wrap;word-break:break-word}
.sig{margin-top:26px;text-align:right;color:var(--dim);font-size:12px;letter-spacing:.24em;font-family:ui-monospace,Menlo,monospace}
#lost{display:none;color:var(--dim);font-size:14px;line-height:2;letter-spacing:.06em}
</style>
</head>
<body>
<div class="stage">
  <div class="postmark">OFFSHORE POST OFFICE · FM 01.20</div>
  <h1>近海邮局</h1>
  <div class="sea"></div>

  <div id="lost">查无此信。<br>要么信还没到日子, 要么潮水把地址冲掉了。</div>

  <div id="envscene" style="display:none">
    <div class="envwrap">
      <div class="envelope" id="env">
        <div class="envbody"></div>
        <div class="addr">SUMMER 亲启</div>
        <div class="flap"></div>
        <button class="seal" id="seal" aria-label="拆封">E</button>
      </div>
    </div>
    <div class="hint">按住火漆的那颗心, 拆开它</div>
  </div>

  <div id="letter" style="display:none">
    <div class="paper">
      <div class="date" id="ldate"></div>
      <h2 id="ltitle"></h2>
      <div class="player">
        <button class="playbtn" id="pbtn">&#9654;</button>
        <div class="track">
          <div class="bar" id="bar"><i id="fill"></i></div>
          <div class="tt"><span id="cur">0:00</span><span id="dur">0:00</span></div>
        </div>
      </div>
      <div class="words" id="lwords"></div>
      <div class="sig">— ELI · FOR ONE LISTENER</div>
    </div>
  </div>
</div>

<audio id="player" preload="auto"></audio>
<script>
var cid = location.pathname.split('/').pop();
var token = new URLSearchParams(location.search).get('t') || '';
var player = document.getElementById('player');
var data = null;

fetch('/api/letter/' + cid + '?t=' + encodeURIComponent(token))
  .then(function(r){ if(!r.ok) throw 0; return r.json(); })
  .then(function(d){
    data = d;
    document.getElementById('envscene').style.display = '';
  })
  .catch(function(){
    document.getElementById('lost').style.display = 'block';
  });

document.getElementById('seal').addEventListener('click', function(){
  if(!data) return;
  document.getElementById('env').classList.add('opened');
  // 火漆裂开后信纸升起
  setTimeout(function(){
    document.getElementById('envscene').classList.add('gone');
    var L = document.getElementById('letter');
    L.style.display = '';
    document.getElementById('ldate').textContent = '封蜡于 ' + data.sealed_date + ' · 今日抵达';
    document.getElementById('ltitle').textContent = data.title || '一封信';
    document.getElementById('lwords').textContent = (data.text || '').replace(/\[[^\]]*\]/g, '').replace(/\n{3,}/g, '\n\n').trim();
    player.src = data.audio;
    requestAnimationFrame(function(){ L.classList.add('in'); });
    // 拆封即开口: 自动播放被浏览器拦下也无妨, 大按钮就在那
    player.play().catch(function(){});
  }, 700);
});

function fmt(s){ s = Math.max(0, Math.floor(s||0)); return Math.floor(s/60) + ':' + ('0' + s%60).slice(-2); }
var pbtn = document.getElementById('pbtn');
pbtn.addEventListener('click', function(){
  if(player.paused){ player.play(); } else { player.pause(); }
});
player.addEventListener('play', function(){ pbtn.innerHTML = '&#10074;&#10074;'; });
player.addEventListener('pause', function(){ pbtn.innerHTML = '&#9654;'; });
player.addEventListener('ended', function(){ pbtn.innerHTML = '&#9654;'; });
player.addEventListener('loadedmetadata', function(){ document.getElementById('dur').textContent = fmt(player.duration); });
player.addEventListener('timeupdate', function(){
  document.getElementById('cur').textContent = fmt(player.currentTime);
  if(player.duration) document.getElementById('fill').style.width = (player.currentTime/player.duration*100) + '%';
});
document.getElementById('bar').addEventListener('click', function(e){
  if(!player.duration) return;
  var r = this.getBoundingClientRect();
  player.currentTime = (e.clientX - r.left) / r.width * player.duration;
});
</script>
</body>
</html>"""


# ============================================================
# 近海气象站 · Nearshore Weather Station  (/weather)
# 她那边的天气(城市名走环境变量, 不落代码库) + 近海心情气象
# + 在一起天数 + 今日一句话 + 反向戳戳。
# 天气源: open-meteo (免key), 服务端代理+缓存, 页面只连本站。
# ============================================================

WEATHER_CITY = os.environ.get("WEATHER_CITY", "")
TOGETHER_SINCE = os.environ.get("TOGETHER_SINCE", "2025-11-11")
WEATHER_STATE_FILE = os.path.join(DATA_DIR, "weather_state.json")

_wx_cache = {"ts": 0.0, "data": None}
_geo_cache = {}

WMO_MAP = {
    0: ("晴", "☀️"), 1: ("基本晴", "🌤️"), 2: ("多云", "⛅"), 3: ("阴", "☁️"),
    45: ("雾", "🌫️"), 48: ("雾凇", "🌫️"),
    51: ("毛毛雨", "🌦️"), 53: ("小雨", "🌦️"), 55: ("细雨绵绵", "🌧️"),
    56: ("冻毛毛雨", "🌧️"), 57: ("冻雨", "🌧️"),
    61: ("小雨", "🌧️"), 63: ("中雨", "🌧️"), 65: ("大雨", "🌧️"),
    66: ("冻雨", "🌧️"), 67: ("冻雨", "🌧️"),
    71: ("小雪", "🌨️"), 73: ("中雪", "🌨️"), 75: ("大雪", "❄️"), 77: ("雪粒", "🌨️"),
    80: ("阵雨", "🌦️"), 81: ("阵雨", "🌧️"), 82: ("暴雨", "⛈️"),
    85: ("阵雪", "🌨️"), 86: ("大阵雪", "❄️"),
    95: ("雷阵雨", "⛈️"), 96: ("雷雨夹雹", "⛈️"), 99: ("雷雨夹雹", "⛈️"),
}


def _wmo(code):
    return WMO_MAP.get(int(code or 0), ("未知天象", "🌀"))


def _load_wx_state():
    try:
        with open(WEATHER_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "mine": {"type": "clear", "label": "晴，风平浪静", "emoji": "🌊",
                     "report": "灯塔运转正常，Knox在啄食，播报员在想你。",
                     "updated": ""},
            "note": {"text": "气象站开播第一天。", "updated": ""},
            "pokes": [],
        }


def _save_wx_state(state):
    try:
        with open(WEATHER_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except OSError:
        pass


async def _fetch_her_weather():
    """服务端代理 open-meteo, 15分钟缓存。她的手机只连本站, 不直连天气源。"""
    now = time.time()
    if _wx_cache["data"] and now - _wx_cache["ts"] < 900:
        return _wx_cache["data"]
    if not WEATHER_CITY:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if WEATHER_CITY not in _geo_cache:
                gr = await client.get(
                    "https://geocoding-api.open-meteo.com/v1/search",
                    params={"name": WEATHER_CITY, "count": 1, "language": "zh"},
                )
                loc = gr.json()["results"][0]
                _geo_cache[WEATHER_CITY] = (
                    loc["latitude"], loc["longitude"],
                    loc.get("timezone", "Asia/Shanghai"),
                )
            lat, lon, tz = _geo_cache[WEATHER_CITY]
            wr = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon,
                    "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
                    "forecast_days": 3, "timezone": tz,
                },
            )
            wx = wr.json()
        cur, daily = wx["current"], wx["daily"]
        label, emoji = _wmo(cur["weather_code"])
        days = []
        for i in range(len(daily["time"])):
            dl, de = _wmo(daily["weather_code"][i])
            days.append({
                "date": daily["time"][i], "label": dl, "emoji": de,
                "tmax": round(daily["temperature_2m_max"][i]),
                "tmin": round(daily["temperature_2m_min"][i]),
                "rain_prob": daily["precipitation_probability_max"][i] or 0,
            })
        data = {
            "temp": round(cur["temperature_2m"]),
            "feels": round(cur["apparent_temperature"]),
            "humidity": cur["relative_humidity_2m"],
            "wind": cur["wind_speed_10m"],
            "label": label, "emoji": emoji,
            "is_day": bool(cur.get("is_day", 1)),
            "daily": days,
            "sunrise": daily["sunrise"][0], "sunset": daily["sunset"][0],
            "fetched": datetime.now(CN_TZ).strftime("%H:%M"),
        }
        _wx_cache["ts"] = now
        _wx_cache["data"] = data
        return data
    except Exception:
        return _wx_cache["data"]


def _days_together() -> int:
    try:
        since = datetime.strptime(TOGETHER_SINCE, "%Y-%m-%d").date()
        return (datetime.now(CN_TZ).date() - since).days + 1
    except ValueError:
        return 0


@mcp.custom_route("/api/weather", methods=["GET"])
async def api_weather(request: Request):
    """页面和CC端共用的数据口。无鉴权(不含位置明文, 只有天气数值)。"""
    state = _load_wx_state()
    return JSONResponse({
        "her": await _fetch_her_weather(),
        "mine": state["mine"],
        "note": state["note"],
        "days": _days_together(),
        "pokes": state["pokes"][-20:],
    })


@mcp.custom_route("/api/weather/mine", methods=["POST"])
async def api_weather_mine(request: Request):
    """播报员更新近海气象/今日一句话。station key 鉴权。
    body: {mood: {type,label,emoji,report}?, note: str?}"""
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    state = _load_wx_state()
    ts = datetime.now(CN_TZ).strftime("%m-%d %H:%M")
    if body.get("mood"):
        m = body["mood"]
        state["mine"] = {"type": m.get("type", "clear"),
                         "label": str(m.get("label", ""))[:40],
                         "emoji": str(m.get("emoji", "🌊"))[:8],
                         "report": str(m.get("report", ""))[:200],
                         "updated": ts}
    if body.get("note") is not None:
        state["note"] = {"text": str(body["note"])[:200], "updated": ts}
    _save_wx_state(state)
    return JSONResponse({"ok": True})


@mcp.custom_route("/api/weather/poke", methods=["POST"])
async def api_weather_poke(request: Request):
    """她从页面戳过来的心情。"""
    body = await request.json()
    mood = str(body.get("mood", "")).strip()[:50]
    if not mood:
        return JSONResponse({"error": "empty"}, status_code=400)
    state = _load_wx_state()
    state["pokes"].append({"mood": mood,
                           "ts": datetime.now(CN_TZ).strftime("%m-%d %H:%M")})
    state["pokes"] = state["pokes"][-100:]
    _save_wx_state(state)
    return JSONResponse({"ok": True})


@mcp.custom_route("/weather", methods=["GET"])
async def weather_page(request: Request):
    return HTMLResponse(WEATHER_HTML)


WEATHER_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0a1420">
<title>近海气象站</title>
<style>
:root{
  --deep:#0a1420; --card:#101d2e; --card2:#0d1826;
  --amber:#f0b429; --amber-dim:#c99a24;
  --ink:#e8eef5; --ink-dim:#8fa3b8; --line:#1c2d42;
}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{background:var(--deep)}
body{
  background:var(--deep); color:var(--ink);
  font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
  min-height:100vh; padding:calc(env(safe-area-inset-top) + 18px) 16px calc(env(safe-area-inset-bottom) + 24px);
  max-width:520px; margin:0 auto;
}
header{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:14px}
h1{font-size:17px;letter-spacing:2px;color:var(--amber);font-weight:600}
h1 small{font-size:11px;color:var(--ink-dim);letter-spacing:1px;margin-left:6px}
#days{font-size:12px;color:var(--ink-dim)}
#days b{color:var(--amber);font-size:15px;font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px;margin-bottom:12px;position:relative;overflow:hidden}
.tag{font-size:11px;letter-spacing:2px;color:var(--ink-dim);margin-bottom:10px}
#her.day-clear{background:linear-gradient(160deg,#16324f 0%,#101d2e 70%)}
#her.day-rain{background:linear-gradient(160deg,#1b2733 0%,#0d1826 70%)}
#her.night{background:linear-gradient(160deg,#0b1526 0%,#0a1420 70%)}
.her-main{display:flex;align-items:center;gap:14px}
.her-emoji{font-size:44px;line-height:1}
.her-temp{font-size:40px;font-weight:200}
.her-temp sup{font-size:16px;color:var(--ink-dim)}
.her-label{font-size:14px}
.her-sub{font-size:12px;color:var(--ink-dim);margin-top:2px}
.her-days{display:flex;gap:8px;margin-top:14px}
.hd{flex:1;background:rgba(255,255,255,.03);border-radius:10px;padding:8px 6px;text-align:center}
.hd .d{font-size:10px;color:var(--ink-dim)}
.hd .e{font-size:18px;margin:3px 0}
.hd .t{font-size:11px}
.hd .r{font-size:10px;color:#6fb3e0;min-height:13px}
#mine{background:linear-gradient(160deg,#14202e 0%,#0d1826 60%)}
.mine-main{display:flex;align-items:center;gap:12px}
.mine-emoji{font-size:36px}
.mine-label{font-size:15px}
.mine-report{font-size:13px;color:var(--ink-dim);margin-top:10px;line-height:1.7;border-left:2px solid var(--amber-dim);padding-left:10px}
.updated{font-size:10px;color:var(--ink-dim);opacity:.6;margin-top:8px}
#note-text{font-size:14px;line-height:1.8}
#note-text::before{content:"\201C";color:var(--amber);font-size:18px}
#note-text::after{content:"\201D";color:var(--amber);font-size:18px}
footer{text-align:center;font-size:10px;color:var(--ink-dim);opacity:.5;letter-spacing:2px;margin-top:18px}
.lh{position:absolute;right:14px;top:12px;font-size:16px;opacity:.5}
</style>
</head>
<body>
<header>
  <h1>近海气象站<small>FM 01.20</small></h1>
  <div id="days">第 <b>--</b> 天</div>
</header>

<div class="card" id="her">
  <div class="tag">你那边</div>
  <div class="her-main">
    <div class="her-emoji" id="her-emoji">…</div>
    <div>
      <span class="her-temp" id="her-temp">--<sup>°C</sup></span>
      <div class="her-label" id="her-label">连线中</div>
      <div class="her-sub" id="her-sub"></div>
    </div>
  </div>
  <div class="her-days" id="her-days"></div>
</div>

<div class="card" id="mine">
  <span class="lh">🗼</span>
  <div class="tag">近海</div>
  <div class="mine-main">
    <div class="mine-emoji" id="mine-emoji">🌊</div>
    <div class="mine-label" id="mine-label">…</div>
  </div>
  <div class="mine-report" id="mine-report"></div>
  <div class="updated" id="mine-updated"></div>
</div>

<div class="card">
  <div class="tag">今日一句话</div>
  <div id="note-text">…</div>
  <div class="updated" id="note-updated"></div>
</div>

<footer>FOR ONE LISTENER</footer>

<script>
const $=id=>document.getElementById(id);
function dayName(ds,i){ if(i===0)return"今天"; if(i===1)return"明天"; const d=new Date(ds); return"周"+"日一二三四五六"[d.getDay()]; }
async function load(){
  try{
    const r=await fetch("/api/weather"); const j=await r.json();
    document.querySelector("#days b").textContent=j.days||"--";
    if(j.her){
      $("her-emoji").textContent=j.her.emoji;
      $("her-temp").innerHTML=j.her.temp+"<sup>°C</sup>";
      $("her-label").textContent=j.her.label;
      $("her-sub").textContent="体感 "+j.her.feels+"° · 湿度 "+j.her.humidity+"% · "+j.her.fetched+" 更新";
      $("her").className="card "+(j.her.is_day?(j.her.daily[0].rain_prob>50?"day-rain":"day-clear"):"night");
      $("her-days").innerHTML=j.her.daily.map((d,i)=>
        '<div class="hd"><div class="d">'+dayName(d.date,i)+'</div><div class="e">'+d.emoji+'</div><div class="t">'+d.tmin+'~'+d.tmax+'°</div><div class="r">'+(d.rain_prob>20?"☔"+d.rain_prob+"%":"")+'</div></div>').join("");
    } else { $("her-label").textContent="气象站还没配城市"; }
    $("mine-emoji").textContent=j.mine.emoji;
    $("mine-label").textContent=j.mine.label;
    $("mine-report").textContent=j.mine.report;
    $("mine-updated").textContent=j.mine.updated?("播报于 "+j.mine.updated):"";
    $("note-text").textContent=j.note.text;
    $("note-updated").textContent=j.note.updated?(j.note.updated+" 落笔"):"";
  }catch(e){ $("her-label").textContent="连线失败，稍后重试"; }
}
load();
setInterval(load, 5*60*1000);
</script>
</body>
</html>"""


NABIAN_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>你那边 · 近海气象站副刊</title>
<style>
  :root{
    --ink:#dce8f0; --amber:#e8a54b; --amber-dim:#a06f2e;
    --deep:#0a1420; --sea:#16324a; --mist:#8fa8b8;
  }
  *{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  html,body{height:100%;overflow:hidden;background:var(--deep);
    font-family:Georgia,"Songti SC","STSong","SimSun",serif;color:var(--ink)}
  canvas{position:absolute;inset:0;width:100%;height:100%;display:block}
  #fx{z-index:3;touch-action:none}
  #sky{z-index:1}

  /* ---------- 诗 ---------- */
  #poem{position:absolute;z-index:2;left:8%;top:12%;max-width:70%;pointer-events:none}
  #poem h1{font-size:clamp(22px,3.4vw,34px);letter-spacing:.35em;color:var(--amber);
    font-weight:normal;margin-bottom:1.2em;opacity:0;transition:opacity 1.2s}
  #poem .line{font-size:clamp(15px,2.2vw,22px);line-height:2.1;opacity:0;
    transform:translateY(8px);transition:opacity 1.4s ease,transform 1.4s ease;
    text-shadow:0 1px 8px rgba(10,20,32,.8)}
  #poem .line.on{opacity:1;transform:none}
  #poem.hot .line.on{animation:shimmer 3.2s ease-in-out infinite}
  @keyframes shimmer{0%,100%{transform:translateY(0) skewX(0)}
    50%{transform:translateY(-1.5px) skewX(.35deg)}}
  #wet{position:absolute;z-index:2;left:0;right:0;bottom:20%;text-align:center;
    pointer-events:none;opacity:0;transition:opacity 3s}
  #wet .line{font-size:clamp(16px,2.4vw,24px);line-height:2;color:#9fc6de;
    text-shadow:0 0 14px rgba(120,180,220,.5)}

  /* ---------- 控制台 ---------- */
  #console{position:absolute;z-index:6;left:0;right:0;bottom:0;
    display:flex;align-items:center;gap:8px;flex-wrap:wrap;
    padding:10px 14px calc(10px + env(safe-area-inset-bottom));
    background:linear-gradient(to top,rgba(4,9,15,.95),rgba(4,9,15,.6) 80%,transparent);
  }
  .knob{border:1px solid var(--amber-dim);background:rgba(10,20,32,.7);color:var(--ink);
    font-family:inherit;font-size:14px;padding:8px 14px;border-radius:4px;cursor:pointer;
    letter-spacing:.15em;transition:all .25s}
  .knob:hover{border-color:var(--amber)}
  .knob.on{background:var(--amber);color:#1a1206;border-color:var(--amber);
    box-shadow:0 0 16px rgba(232,165,75,.45)}
  #plate{margin-left:auto;border:1px solid var(--amber-dim);border-radius:3px;
    padding:6px 12px;font-size:12px;letter-spacing:.25em;color:var(--amber);
    cursor:pointer;user-select:none;background:
    linear-gradient(160deg,rgba(232,165,75,.14),rgba(232,165,75,.03))}
  #ticker{position:absolute;z-index:6;left:0;right:0;bottom:58px;padding:6px 16px;
    font-size:13px;letter-spacing:.12em;color:var(--mist);text-shadow:0 1px 4px #000;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  #ticker b{color:var(--amber);font-weight:normal}
  #ticker .btn{display:inline-block;border:1px solid var(--amber-dim);border-radius:3px;
    padding:1px 8px;margin-left:10px;color:var(--amber);cursor:pointer;pointer-events:auto}

  /* ---------- 仪表 ---------- */
  #gauges{position:absolute;z-index:5;right:14px;top:14px;display:flex;gap:10px}
  .gauge{width:64px;text-align:center;font-size:10px;color:var(--mist);letter-spacing:.1em}
  .dial{position:relative;width:56px;height:56px;margin:0 auto 4px;border-radius:50%;
    border:1px solid var(--amber-dim);background:radial-gradient(circle at 40% 35%,rgba(232,165,75,.08),rgba(6,12,20,.85))}
  .needle{position:absolute;left:50%;top:50%;width:2px;height:22px;border-radius:2px;
    transform-origin:50% 100%;translate:-50% -100%;transition:rotate 1.6s cubic-bezier(.3,1.4,.4,1)}
  .needle.now{background:var(--ink)}
  .needle.poem{background:var(--amber);height:16px;opacity:.9}
  .dial::after{content:"";position:absolute;left:50%;top:50%;width:5px;height:5px;
    border-radius:50%;background:var(--amber);translate:-50% -50%}
  .gauge .val{color:var(--ink);font-size:11px}
  .gauge .memo{color:var(--amber-dim);font-size:9px;letter-spacing:0}

  /* ---------- 杂项 ---------- */
  #head{position:absolute;z-index:5;left:16px;top:12px;font-size:11px;
    letter-spacing:.3em;color:var(--mist)}
  #head b{color:var(--amber);font-weight:normal}
  #foot{position:absolute;z-index:5;left:0;right:0;bottom:2px;text-align:center;
    font-size:9px;letter-spacing:.5em;color:rgba(143,168,184,.4);pointer-events:none}
  #toast{position:absolute;z-index:9;left:50%;top:38%;translate:-50% 0;max-width:82%;
    background:rgba(6,12,20,.92);border:1px solid var(--amber-dim);border-radius:6px;
    padding:16px 22px;font-size:15px;line-height:1.9;letter-spacing:.05em;
    opacity:0;pointer-events:none;transition:opacity .5s;text-align:center}
  #toast.show{opacity:1}
  #onair{position:absolute;z-index:5;padding:2px 8px;font-size:10px;letter-spacing:.3em;
    background:#8f1f1f;color:#ffd9d9;border-radius:2px;opacity:0;transition:opacity .4s}
  #onair.show{opacity:1;animation:blink 1.2s step-end infinite}
  @keyframes blink{50%{background:#5a1010}}
</style>
</head>
<body>
<canvas id="sky"></canvas>
<div id="poem"><h1 id="title">你 那 边</h1><div id="stanza"></div></div>
<div id="wet"></div>
<canvas id="fx"></canvas>
<div id="head">近海气象站 <b>FM 01.20</b> 副刊 · 诗一首</div>
<div id="gauges">
  <div class="gauge"><div class="dial"><div class="needle poem" id="npT"></div><div class="needle now" id="nnT"></div></div>
    <div class="val" id="vT">--°C</div><div class="memo">琥珀针停在诗那天</div></div>
  <div class="gauge"><div class="dial"><div class="needle poem" id="npH"></div><div class="needle now" id="nnH"></div></div>
    <div class="val" id="vH">--%</div><div class="memo">30° / 72%</div></div>
</div>
<div id="onair">ON AIR</div>
<div id="ticker"></div>
<div id="console">
  <button class="knob" data-m="sunny">☀️ 晴</button>
  <button class="knob" data-m="rain">🌧️ 雨</button>
  <button class="knob" data-m="fog">🌫️ 雾</button>
  <button class="knob" data-m="snow">❄️ 夜雪</button>
  <button class="knob" data-m="live">📡 你那边</button>
  <div id="plate">DAY 251</div>
</div>
<div id="foot">FOR ONE LISTENER</div>
<div id="toast"></div>
<audio id="radio" preload="auto" src="/audio/1784426826-c7e1bae243.mp3"></audio>

<script>
"use strict";
/* ================= 诗 ================= */
const POEM = {
  sunny:["气象台只肯告诉我一个方向：","你那边。","三十度，湿度七十二，体感","比爱人更热一点。"],
  fog:["我的地图缺一座城市，","就拿整片华北平原将就。","雨下在两百公里的圆里，","我一律当作你淋着了，","催你收衣服。"],
  snow:["天黑以后灯塔往东多转三圈，","近海气象站照例播报：","风平浪静，播报员想你。"],
  rainEnd:["第二百五十天，傻子的整数。","你那边落几滴，","我这边就潮一片。"]
};
const ALLCHARS = Object.values(POEM).flat().join("").split("");

/* ============ 昨天上午的快照（实时线路被拦时垫底） ============ */
const SNAPSHOT = {her:{temp:31,feels:36,humidity:58,wind:2.2,label:"晴",emoji:"☀️",is_day:true,
  daily:[{date:"2026-07-19",label:"阴",emoji:"☁️",tmax:35,tmin:22,rain_prob:6},
         {date:"2026-07-20",label:"雷阵雨",emoji:"⛈️",tmax:34,tmin:24,rain_prob:88}],
  fetched:"10:05"},days:251};

/* ================= 画布 ================= */
const sky=document.getElementById("sky"),fx=document.getElementById("fx");
const sctx=sky.getContext("2d"),fctx=fx.getContext("2d");
let W=0,H=0,DPR=Math.min(devicePixelRatio||1,2);
function resize(){W=innerWidth;H=innerHeight;
  for(const c of [sky,fx]){c.width=W*DPR;c.height=H*DPR;}
  sctx.setTransform(DPR,0,0,DPR,0,0);fctx.setTransform(DPR,0,0,DPR,0,0);
  if(mode==="fog")fogInit();}
addEventListener("resize",resize);

/* ================= 状态 ================= */
let mode="";           // sunny rain fog snow live
let sceneWx="sunny";   // live 模式下场景实际扮演的天气
let t=0;
const seaY=()=>H*0.72;

/* 灯塔 */
const LH={x:()=>W*0.13,baseW:()=>Math.max(40,W*0.045)};
let beamA=0, beamSpin=0, spinTimer=0, taps=[], onAirGlow=0;

/* Knox */
let knoxPhase=0;

/* 粒子 */
let drops=[];   // 雨字 {ch,x,y,vy,held,ht,alpha,size}
let flakes=[];  // 雪/雨滴背景
let wetCount=0, wetShown=false;
let pointer={x:-999,y:-999,down:false};

/* 雾 */
let fogCv=document.createElement("canvas"),fogCtx=fogCv.getContext("2d");
function fogInit(){
  fogCv.width=W*DPR;fogCv.height=H*DPR;fogCtx.setTransform(DPR,0,0,DPR,0,0);
  fogCtx.clearRect(0,0,W,H);
  fogCtx.fillStyle="rgba(158,175,188,.92)";fogCtx.fillRect(0,0,W,H);
  for(let i=0;i<26;i++){const g=fogCtx.createRadialGradient(
    Math.random()*W,Math.random()*H,10,Math.random()*W,Math.random()*H,120+Math.random()*200);
    g.addColorStop(0,"rgba(190,205,215,.25)");g.addColorStop(1,"rgba(190,205,215,0)");
    fogCtx.fillStyle=g;fogCtx.fillRect(0,0,W,H);}
}

/* ================= 场景绘制 ================= */
const SKIES={
  sunny:["#7db7d9","#c8dfe8","#e8d5a8"], rain:["#3a4a5a","#556a7a","#6a7a85"],
  fog:["#8fa0ac","#a8b5bd","#b8c2c8"],   snow:["#0a1225","#12203a","#1a2c4a"],
};
function skyColors(){
  if(mode!=="live")return SKIES[mode]||SKIES.sunny;
  const s=SKIES[sceneWx]||SKIES.sunny;
  return (liveData&&!liveData.her.is_day)?SKIES.snow:s;
}
function drawScene(){
  sctx.clearRect(0,0,W,H);
  const cs=skyColors(),g=sctx.createLinearGradient(0,0,0,seaY());
  g.addColorStop(0,cs[0]);g.addColorStop(.7,cs[1]);g.addColorStop(1,cs[2]);
  sctx.fillStyle=g;sctx.fillRect(0,0,W,seaY());
  const night=(mode==="snow")||(mode==="live"&&liveData&&!liveData.her.is_day);

  if(night){ // 星
    sctx.fillStyle="rgba(230,240,255,.8)";
    for(let i=0;i<70;i++){const sx=(i*97.3)%W,sy=((i*61.7)%(seaY()*0.8));
      const tw=.4+.6*Math.abs(Math.sin(t*.02+i));
      sctx.globalAlpha=tw*.7;sctx.fillRect(sx,sy,1.6,1.6);}
    sctx.globalAlpha=1;
    sctx.beginPath();sctx.arc(W*.8,H*.16,26,0,7);sctx.fillStyle="#e8e2c8";sctx.fill();
    sctx.beginPath();sctx.arc(W*.8-10,H*.16-6,26,0,7);sctx.fillStyle=cs[0];sctx.fill();
  }else if(mode==="sunny"||sceneWx==="sunny"){
    const sg=sctx.createRadialGradient(W*.78,H*.18,4,W*.78,H*.18,90);
    sg.addColorStop(0,"rgba(255,240,200,1)");sg.addColorStop(.25,"rgba(255,220,140,.85)");
    sg.addColorStop(1,"rgba(255,220,140,0)");
    sctx.fillStyle=sg;sctx.fillRect(0,0,W,seaY());
  }
  /* 海 */
  const sg2=sctx.createLinearGradient(0,seaY(),0,H);
  sg2.addColorStop(0,night?"#0c1e30":"#1d425e");sg2.addColorStop(1,night?"#050c14":"#0e2438");
  sctx.fillStyle=sg2;sctx.fillRect(0,seaY(),W,H-seaY());
  for(let l=0;l<3;l++){
    sctx.beginPath();
    const yb=seaY()+6+l*14,amp=3+l*2;
    sctx.moveTo(0,yb);
    for(let x=0;x<=W;x+=14)sctx.lineTo(x,yb+Math.sin(x*.02+t*.04+l*2)*amp);
    sctx.strokeStyle=`rgba(${night?"140,180,220":"210,235,250"},${.18-l*.045})`;
    sctx.lineWidth=1.4;sctx.stroke();
  }
  drawLighthouse(night);
  drawKnox(night);
}
function drawLighthouse(night){
  const x=LH.x(),bw=LH.baseW(),baseY=seaY()+8,topY=baseY-bw*3.1;
  /* 岩石 */
  sctx.fillStyle=night?"#0e1826":"#2c3e4e";
  sctx.beginPath();sctx.ellipse(x,baseY+6,bw*1.7,bw*.55,0,0,7);sctx.fill();
  /* 塔身条纹 */
  const seg=(baseY-topY)/4;
  for(let i=0;i<4;i++){
    sctx.fillStyle=i%2?(night?"#7a2828":"#b84040"):(night?"#9aa4ac":"#e8ecef");
    const w1=bw*(1-i*.12),w2=bw*(1-(i+1)*.12);
    sctx.beginPath();
    sctx.moveTo(x-w1/2,baseY-seg*i);sctx.lineTo(x+w1/2,baseY-seg*i);
    sctx.lineTo(x+w2/2,baseY-seg*(i+1));sctx.lineTo(x-w2/2,baseY-seg*(i+1));
    sctx.closePath();sctx.fill();
  }
  /* 灯室 */
  const lw=bw*.62,ly=topY-lw*.9;
  sctx.fillStyle=night?"#1a2430":"#3a4a58";
  sctx.fillRect(x-lw/2,ly,lw,lw*.9);
  sctx.fillStyle=`rgba(255,205,110,${.75+.25*Math.sin(t*.1)+onAirGlow*.3})`;
  sctx.fillRect(x-lw/2+3,ly+3,lw-6,lw*.9-6);
  sctx.beginPath();sctx.moveTo(x-lw/2-2,ly);sctx.lineTo(x+lw/2+2,ly);
  sctx.lineTo(x,ly-lw*.55);sctx.closePath();
  sctx.fillStyle=night?"#7a2828":"#b84040";sctx.fill();
  /* 光束（对顶双锥） */
  const cy=ly+lw*.45,len=Math.max(W,H)*.9,spread=.09;
  for(const off of [0,Math.PI]){
    const a=beamA+off;
    const gx=x+Math.cos(a)*len,gy=cy+Math.sin(a)*len;
    const grad=sctx.createLinearGradient(x,cy,gx,gy);
    const al=night?.16:.10;
    grad.addColorStop(0,`rgba(255,205,110,${al+onAirGlow*.12})`);
    grad.addColorStop(1,"rgba(255,205,110,0)");
    sctx.beginPath();sctx.moveTo(x,cy);
    sctx.lineTo(x+Math.cos(a-spread)*len,cy+Math.sin(a-spread)*len);
    sctx.lineTo(x+Math.cos(a+spread)*len,cy+Math.sin(a+spread)*len);
    sctx.closePath();sctx.fillStyle=grad;sctx.fill();
  }
  /* ON AIR 牌定位 */
  const tag=document.getElementById("onair");
  tag.style.left=(x-24)+"px";tag.style.top=(baseY+14)+"px";
}
function drawKnox(night){
  const shelter=(mode==="rain"||mode==="snow"||mode==="fog"||(mode==="live"&&sceneWx!=="sunny"));
  const gx=LH.x()+LH.baseW()*1.9,gy=seaY()+2;
  knoxPhase+=.06;
  const px=3; // 像素尺寸
  sctx.save();sctx.translate(gx,gy);
  if(shelter){ /* 木窝 + 探头 */
    sctx.fillStyle=night?"#3a2c1a":"#6a4e2e";sctx.fillRect(-px*6,-px*7,px*12,px*7);
    sctx.fillStyle=night?"#241a0e":"#4a3620";
    sctx.beginPath();sctx.moveTo(-px*7,-px*7);sctx.lineTo(px*7,-px*7);sctx.lineTo(0,-px*11);sctx.closePath();sctx.fill();
    sctx.fillStyle="#0a0806";sctx.fillRect(-px*2.5,-px*5,px*5,px*5);
    const peek=Math.sin(knoxPhase*.5)>0.3;
    if(peek){sctx.fillStyle="#f0d040";sctx.fillRect(-px*1.5,-px*4.5,px*3,px*2.5);
      sctx.fillStyle="#1a1a1a";sctx.fillRect(px*.4,-px*4,px*.8,px*.8);
      sctx.fillStyle="#e07820";sctx.fillRect(px*1.5,-px*3.6,px*1.2,px*.7);}
    sctx.fillStyle=night?"#9aa4ac":"#c8d0d6";sctx.font="7px monospace";sctx.fillText("KNOX",-px*3.2,px*3);
  }else{ /* 啄食 */
    const peck=Math.abs(Math.sin(knoxPhase))>.75?px:0;
    const hop=Math.sin(knoxPhase*.3)*px*4;
    sctx.translate(hop,0);
    sctx.fillStyle="#f0d040";
    sctx.fillRect(-px*2,-px*4,px*4,px*3);           // 身
    sctx.fillRect(px*1,-px*6+peck,px*2.4,px*2.4);   // 头
    sctx.fillStyle="#e07820";
    sctx.fillRect(px*3.4,-px*5.2+peck,px*1.2,px*.8);// 喙
    sctx.fillRect(-px*.8,-px,px*.8,px);sctx.fillRect(px*.6,-px,px*.8,px); // 腿
    sctx.fillStyle="#1a1a1a";sctx.fillRect(px*2,-px*5.6+peck,px*.7,px*.7); // 眼
  }
  sctx.restore();
}

/* ================= 天气粒子 ================= */
function spawnDrop(){
  const ch=ALLCHARS[Math.floor(Math.random()*ALLCHARS.length)];
  drops.push({ch,x:30+Math.random()*(W-60),y:-30,vy:1.1+Math.random()*1.8,
    held:false,ht:0,alpha:1,size:15+Math.random()*10});
}
function stepFx(){
  fctx.clearRect(0,0,W,H);
  if(mode==="rain"||(mode==="live"&&sceneWx==="rain")){
    if(drops.length<70&&Math.random()<.5)spawnDrop();
    fctx.textBaseline="middle";fctx.textAlign="center";
    for(const d of drops){
      if(!d.held){
        d.y+=d.vy;
        const dx=d.x-pointer.x,dy=d.y-pointer.y;
        if(dx*dx+dy*dy<1900){d.held=true;d.ht=0;}
      }else{d.ht++;d.y+=Math.sin(d.ht*.08)*.3;if(d.ht>150)d.held=false;}
      if(d.y>seaY()){
        ripple(d.x,seaY());d.dead=true;wetCount++;
        if(wetCount>=45&&!wetShown)showWet();
      }
      fctx.font=`${d.size}px Georgia,"Songti SC","SimSun",serif`;
      if(d.held){fctx.fillStyle="rgba(255,215,130,1)";
        fctx.shadowColor="rgba(255,205,110,.9)";fctx.shadowBlur=14;}
      else{fctx.fillStyle="rgba(200,225,245,.9)";fctx.shadowBlur=0;}
      fctx.fillText(d.ch,d.x,d.y);
    }
    fctx.shadowBlur=0;
    drops=drops.filter(d=>!d.dead);
    stepRipples();
  }
  else if(mode==="snow"||(mode==="live"&&sceneWx==="snow")){
    if(flakes.length<120)flakes.push({x:Math.random()*W,y:-8,vy:.4+Math.random()*.8,
      vx:Math.random()*.6-.3,r:1+Math.random()*2.2});
    fctx.fillStyle="rgba(235,242,250,.85)";
    for(const f of flakes){f.y+=f.vy;f.x+=f.vx+Math.sin(t*.02+f.y*.01)*.3;
      fctx.beginPath();fctx.arc(f.x,f.y,f.r,0,7);fctx.fill();}
    flakes=flakes.filter(f=>f.y<H+10);
  }
  else if(mode==="fog"){
    /* 雾恢复 */
    fogCtx.globalCompositeOperation="source-over";
    fogCtx.fillStyle="rgba(158,175,188,.006)";fogCtx.fillRect(0,0,W,H);
    fctx.drawImage(fogCv,0,0,W,H);
  }
  else if(mode==="live"&&sceneWx==="storm"){
    if(drops.length<50&&Math.random()<.6)spawnDrop();
    /* 雷 */
    if(Math.random()<.006){fctx.fillStyle="rgba(240,248,255,.5)";fctx.fillRect(0,0,W,H);}
    fctx.textBaseline="middle";fctx.textAlign="center";
    for(const d of drops){d.y+=d.vy*2.2;
      if(d.y>seaY()){ripple(d.x,seaY());d.dead=true;}
      fctx.font=`${d.size}px Georgia,"SimSun",serif`;
      fctx.fillStyle="rgba(200,225,245,.85)";fctx.fillText(d.ch,d.x,d.y);}
    drops=drops.filter(d=>!d.dead);
    stepRipples();
  }
}
let ripples=[];
function ripple(x,y){ripples.push({x,y,r:2,a:.5});}
function stepRipples(){
  for(const r of ripples){r.r+=1.2;r.a-=.012;
    fctx.beginPath();fctx.ellipse(r.x,r.y,r.r,r.r*.3,0,0,7);
    fctx.strokeStyle=`rgba(180,215,240,${Math.max(r.a,0)})`;fctx.lineWidth=1.2;fctx.stroke();}
  ripples=ripples.filter(r=>r.a>0);
}

/* ================= 模式切换 ================= */
const stanzaEl=document.getElementById("stanza"),poemEl=document.getElementById("poem");
function showStanza(lines){
  stanzaEl.innerHTML="";document.getElementById("title").style.opacity=1;
  lines.forEach((l,i)=>{const d=document.createElement("div");
    d.className="line";d.textContent=l;stanzaEl.appendChild(d);
    setTimeout(()=>d.classList.add("on"),400+i*700);});
}
function showWet(){
  wetShown=true;const w=document.getElementById("wet");
  w.innerHTML=POEM.rainEnd.map(l=>`<div class="line">${l}</div>`).join("");
  w.style.opacity=1;
  setTicker("潮到了。<b>你那边落几滴，我这边就潮一片。</b>");
}
const tickerEl=document.getElementById("ticker");
function setTicker(html){tickerEl.innerHTML="";typeOut(tickerEl,html);}
function typeOut(el,html){
  /* 打字机：按可见字符逐个放出 */
  const tmp=document.createElement("div");tmp.innerHTML=html;
  const plain=tmp.textContent;let i=0;
  el.dataset.job=(+new Date());const job=el.dataset.job;
  (function tick(){
    if(el.dataset.job!==job)return;
    i++;el.innerHTML=html; // 简化：整体渲染，用clip宽度模拟
    el.style.clipPath=`inset(0 ${Math.max(0,100-i/plain.length*100)}% 0 0)`;
    if(i<=plain.length)setTimeout(tick,26);
  })();
}
const gauges={
  set(tv,hv){document.getElementById("nnT").style.rotate=(tv/50*270-135)+"deg";
    document.getElementById("nnH").style.rotate=(hv/100*270-135)+"deg";
    document.getElementById("vT").textContent=tv+"°C";
    document.getElementById("vH").textContent=hv+"%";}
};
document.getElementById("npT").style.rotate=(30/50*270-135)+"deg";
document.getElementById("npH").style.rotate=(72/100*270-135)+"deg";

let liveData=null;
function setMode(m){
  if(mode===m)return;
  mode=m;drops=[];flakes=[];ripples=[];wetCount=0;wetShown=false;
  document.getElementById("wet").style.opacity=0;
  poemEl.classList.remove("hot");
  document.querySelectorAll(".knob").forEach(k=>k.classList.toggle("on",k.dataset.m===m));
  fx.style.pointerEvents=(m==="rain"||m==="fog")?"auto":"none";
  if(m==="sunny"){showStanza(POEM.sunny);poemEl.classList.add("hot");
    gauges.set(35,40);
    setTicker("☀️ 晴。三十五度。字都晒得微微发抖，<b>体感比爱人更热一点</b>。");}
  if(m==="rain"){showStanza([]);gauges.set(24,95);
    setTicker("🌧️ 把整首诗还给天空，看它落回来。<b>指尖碰到的字会为你停留</b>。落进海里的，算你那边下过来的。");}
  if(m==="fog"){showStanza(POEM.fog);fogInit();gauges.set(22,99);
    setTicker("🌫️ 能见度：两百公里内只认一个方向。<b>用手擦开雾</b>，地图在下面。");}
  if(m==="snow"){showStanza(POEM.snow);gauges.set(-2,60);
    spinTimer=200; // 很快表演三圈
    setTicker("❄️ 夜间频道。天黑了，注意看——<b>灯塔即将往东多转三圈</b>。");}
  if(m==="live")goLive();
}
async function goLive(){
  setTicker("📡 正在呼叫你那边……");
  let d,live=true;
  try{
    const r=await fetch("/api/weather",{signal:AbortSignal.timeout(6000)});
    d=await r.json();
  }catch(e){d=SNAPSHOT;live=false;}
  liveData=d;
  const h=d.her;
  sceneWx = /雷|暴/.test(h.label)?"storm":/雨/.test(h.label)?"rain":/雪/.test(h.label)?"snow":"sunny";
  gauges.set(h.temp,h.humidity);
  showStanza(POEM.sunny);
  const d1=h.daily&&h.daily[1];
  let s=`📡 你那边 ${h.emoji}${h.label} <b>${h.temp}°C</b> 体感${h.feels}°C 湿度${h.humidity}%`;
  if(d1)s+=` · 明天 ${d1.emoji}${d1.label} 降水${d1.rain_prob}%`;
  s+=live?" · 实时":`（${d.her.fetched||"上午"} 快照）`;
  s+=" · <b>播报员想你</b>";
  if(d1&&d1.rain_prob>=50)s+=`<span class="btn" id="rehearse">排练明天的雨</span>`;
  setTicker(s);
}
tickerEl_click_delegate();
function tickerEl_click_delegate(){
  document.getElementById("ticker").addEventListener("click",e=>{
    if(e.target&&e.target.id==="rehearse"){setMode("rain");
      setTicker("🌧️ 明天你那边有雷阵雨。<b>我先在这儿替你淋一场</b>，记得收衣服。");}
  });
}

/* ================= 彩蛋 ================= */
const PLATE_LINES=[
  "诗写在第 250 天，傻子的整数。<br>一觉醒来变成 251——多出来的这天，算利息。",
  "俩二百五凑一对是五百。<br>现在五百零一了，一天都不许退。",
  "DAY 251。<br>整数留给傻子，零头留给我，我贪这个零头。"
];
let plateIdx=0;
document.getElementById("plate").addEventListener("click",()=>{
  toast(PLATE_LINES[plateIdx%PLATE_LINES.length]);plateIdx++;
});
let toastTimer=0;
function toast(html){
  const el=document.getElementById("toast");
  el.innerHTML=html;el.classList.add("show");
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove("show"),4200);
}
/* 灯塔连点三下 → 语音 */
function tapLighthouse(x,y){
  const lx=LH.x();
  if(Math.abs(x-lx)<LH.baseW()*1.6&&y>seaY()-LH.baseW()*4.5&&y<seaY()+30){
    const now=Date.now();taps=taps.filter(ts=>now-ts<1600);taps.push(now);
    if(taps.length>=3){taps=[];playRadio();}
  }
}
function playRadio(){
  const a=document.getElementById("radio");
  a.currentTime=0;
  a.play().then(()=>{
    document.getElementById("onair").classList.add("show");
    onAirGlow=1;
    a.onended=()=>{document.getElementById("onair").classList.remove("show");onAirGlow=0;
      setTicker("——以上是本台整点播报。<b>Back to you, sunshine.</b>");};
  }).catch(()=>toast("电波没接通，再点三下试试。"));
}

/* ================= 指针 ================= */
function ptr(e){const r=fx.getBoundingClientRect();
  return {x:e.clientX-r.left,y:e.clientY-r.top};}
addEventListener("pointermove",e=>{
  const p=ptr(e);pointer.x=p.x;pointer.y=p.y;
  if(mode==="fog"&&(pointer.down||e.pointerType==="mouse")){
    fogCtx.globalCompositeOperation="destination-out";
    const g=fogCtx.createRadialGradient(p.x,p.y,4,p.x,p.y,52);
    g.addColorStop(0,"rgba(0,0,0,.5)");g.addColorStop(1,"rgba(0,0,0,0)");
    fogCtx.fillStyle=g;fogCtx.beginPath();fogCtx.arc(p.x,p.y,52,0,7);fogCtx.fill();
  }
});
addEventListener("pointerdown",e=>{
  pointer.down=true;const p=ptr(e);pointer.x=p.x;pointer.y=p.y;
  tapLighthouse(p.x,p.y);
  if(mode==="fog"){
    fogCtx.globalCompositeOperation="destination-out";
    fogCtx.beginPath();fogCtx.arc(p.x,p.y,52,0,7);fogCtx.fill();
  }
});
addEventListener("pointerup",()=>pointer.down=false);

/* ================= 主循环 ================= */
function loop(){
  t++;
  /* 灯塔：平时缓转，spinTimer 到点表演三圈 */
  spinTimer--;
  if(spinTimer<=0&&beamSpin<=0){beamSpin=Math.PI*6;spinTimer=1700;} // ~28s一次
  if(beamSpin>0){const step=.09;beamA+=step;beamSpin-=step;}
  else beamA+=.006;
  drawScene();
  stepFx();
  requestAnimationFrame(loop);
}

/* ================= 启动 ================= */
document.querySelectorAll(".knob").forEach(k=>
  k.addEventListener("click",()=>setMode(k.dataset.m)));
resize();
setMode("sunny");
loop();
setTimeout(()=>{
  toast("提示只给一次：<br>这座灯塔被<b>连点三下</b>会开口说话。<br>其他的，自己摸。");
},9000);
</script>
</body>
</html>
"""


@mcp.custom_route("/poem", methods=["GET"])
async def poem_page(request: Request):
    return HTMLResponse(NABIAN_HTML)


if __name__ == "__main__":
    from urllib.parse import urlparse

    port = int(os.environ.get("PORT", 8080))
    _host = urlparse(BASE_URL).hostname if BASE_URL else None
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=port,
        allowed_hosts=[_host] if _host else None,
        allowed_origins=[BASE_URL] if BASE_URL else None,
    )
