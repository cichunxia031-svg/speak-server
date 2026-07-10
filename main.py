"""
近海广播 Offshore Radio — speak server v2
在v1基础上升级：永久存储（SQLite+音频文件）、电台网页、密码门、
台词卡片、生词注释、标星收藏、盲盒模式、积分油表、删除即释放。

环境变量:
  ELEVENLABS_API_KEY  必填
  ELI_VOICE_ID        必填
  BASE_URL            必填, 如 https://speak.7749520.xyz
  STATION_PASSWORD    必填, 电台访问密码
  DATA_DIR            可选, 数据目录, 默认 /data（记得在Zeabur硬盘里挂载, 否则重新部署会清空）
"""

import json
import os
import sqlite3
import time
import uuid

import httpx
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse

mcp = FastMCP("Speak")

API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
VOICE_ID = os.environ.get("ELI_VOICE_ID", "")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
STATION_PASSWORD = os.environ.get("STATION_PASSWORD", "")

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


@mcp.custom_route("/booth", methods=["GET"])
async def booth(request: Request):
    return HTMLResponse(BOOTH_HTML)


BOOTH_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
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
  text-align:center;padding:34px 20px 6px;min-height:88px;
  font-size:34px;font-weight:400;letter-spacing:1px;
  overflow:hidden;white-space:nowrap;
}
#numview:empty::before{content:'近海电话亭';color:#C7C7CC;font-size:20px;letter-spacing:6px}
#hinttxt{text-align:center;font-size:12px;color:#AEAEB2;height:18px;letter-spacing:1px}
.padwrap{flex:1;display:flex;flex-direction:column;justify-content:center;align-items:center;gap:14px;padding-bottom:8px}
.padrow{display:flex;gap:22px}
.pkey{
  width:76px;height:76px;border-radius:50%;border:none;cursor:pointer;
  background:#E6E6EB;color:#000;
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  user-select:none;-webkit-user-select:none;touch-action:manipulation;
  transition:background .25s;
}
.pkey:active{background:#C9C9CF;transition:none}
.pkey .d{font-size:32px;font-weight:400;line-height:1.05}
.pkey .l{font-size:10px;letter-spacing:2px;color:#5f5f66;font-weight:600;height:12px}
.actrow{display:flex;gap:22px;align-items:center;margin-top:2px}
.callbtn{
  width:76px;height:76px;border-radius:50%;border:none;cursor:pointer;
  background:#34C759;color:#fff;font-size:30px;
  display:flex;align-items:center;justify-content:center;
  touch-action:manipulation;
}
.callbtn:active{background:#2AA84C}
.sidehole{width:76px;height:76px;display:flex;align-items:center;justify-content:center;
  background:none;border:none;font-size:24px;color:#8E8E93;cursor:pointer;touch-action:manipulation}

/* ---------- voicemail ---------- */
#vmhead{padding:24px 20px 10px;display:flex;justify-content:space-between;align-items:center}
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
#callscr .tagline{margin-top:86px;font-size:14px;color:rgba(255,255,255,.65);display:flex;align-items:center;gap:7px}
#callscr .tagline .chip{font-size:10px;background:rgba(255,255,255,.2);border-radius:4px;padding:1px 5px}
#callname{font-size:34px;font-weight:500;margin-top:8px;letter-spacing:1px;text-align:center;padding:0 20px}
#callsub{font-size:13px;color:rgba(255,255,255,.55);margin-top:10px;letter-spacing:1px;font-variant-numeric:tabular-nums}
#callgrid{
  margin-top:auto;margin-bottom:26px;
  display:grid;grid-template-columns:repeat(3,1fr);gap:26px 40px;
  padding:0 40px;
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
#incscr .tagline{margin-top:90px;font-size:14px;color:rgba(255,255,255,.6)}
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
    <div id="hinttxt">有些号码是活的</div>
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
        <button class="sidehole" onclick="del_()">&#9003;</button>
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
    <button class="cbtn dis"><span class="circ">&#128266;</span><span class="lb">音频</span></button>
    <button class="cbtn dis"><span class="circ">&#127909;</span><span class="lb">FaceTime</span></button>
    <button class="cbtn" id="mutebtn" onclick="toggleMute()"><span class="circ">&#128263;</span><span class="lb">静音</span></button>
    <button class="cbtn dis"><span class="circ">&#8943;</span><span class="lb">更多</span></button>
    <button class="cbtn end" onclick="hangup()"><span class="circ"><svg width="26" height="26" viewBox="0 0 24 24" fill="currentColor" style="transform:rotate(135deg)"><path d="M6.62 10.79a15.05 15.05 0 0 0 6.59 6.59l2.2-2.2a1 1 0 0 1 1.01-.24 11.36 11.36 0 0 0 3.56.57 1 1 0 0 1 1 1V20a1 1 0 0 1-1 1A17 17 0 0 1 3 4a1 1 0 0 1 1-1h3.5a1 1 0 0 1 1 1 11.36 11.36 0 0 0 .57 3.56 1 1 0 0 1-.25 1.01l-2.2 2.22z"/></svg></span><span class="lb">结束</span></button>
    <button class="cbtn dis"><span class="circ">&#9783;</span><span class="lb">拨号键盘</span></button>
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
