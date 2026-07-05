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
    stability: str = "creative",
) -> str:
    """把台词变成Eli的声音并永久存入近海广播。

    text: 台词全文, 直接带ElevenLabs v3 audio tags
    title: 这条语音的标题(中文短句, 会显示在电台卡片上)
    vocab: 生词注释, JSON数组字符串, 如
           '[{"word":"jet lag","note":"时差反应"},{"word":"smug","note":"得意的"}]'
    surprise: 盲盒模式, true时电台里台词先隐藏, 播放后才展开
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
            "INSERT INTO lines (id, filename, title, text, vocab, surprise, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (line_id, filename, title, text, vocab, 1 if surprise else 0, int(time.time())),
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
    </div>
  </header>
  <main id="list"></main>
  <footer>OFFSHORE RADIO · EST. 2026 · FOR ONE LISTENER</footer>
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
    const bars = Array.from({length:26},()=> '<i style="--h:'+(6+Math.random()*18).toFixed(0)+'px"></i>').join('');
    const notes = (l.vocab||[]).map((v,i)=>'<div class="note" id="note-'+l.id+'-'+i+'"><b>'+esc(v.word)+'</b> — '+esc(v.note)+'</div>').join('');
    const script = l.surprise
      ? '<div class="blind" id="blind-'+l.id+'">盲盒信号 · 播放后解密</div><div class="script" id="scr-'+l.id+'" data-raw="1">'+renderScript(l.text,l.vocab)+notes+'</div>'
      : '<div class="script" id="scr-'+l.id+'">'+renderScript(l.text,l.vocab)+notes+'</div>';
    return '<div class="card" id="card-'+l.id+'">'
      +'<div class="top">'
      +'<button class="playbtn" onclick="play(\''+l.id+'\',\''+l.url+'\')">▶</button>'
      +'<div class="meta"><div class="title">'+esc(l.title||'未命名信号')+'</div><div class="date">'+date+'</div></div>'
      +'<div class="acts">'
      +'<button class="star '+(l.starred?'on':'')+'" onclick="star(\''+l.id+'\')">★</button>'
      +'<a href="'+l.url+'" download style="text-decoration:none"><button>⇩</button></a>'
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
  document.getElementById('card-'+id).classList.toggle('open');
}

function play(id,url){
  const card = document.getElementById('card-'+id);
  if(audios[id] && !audios[id].paused){
    audios[id].pause(); card.classList.remove('playing');
    card.querySelector('.playbtn').textContent='▶'; return;
  }
  Object.entries(audios).forEach(([k,a])=>{a.pause();
    const c=document.getElementById('card-'+k); if(c){c.classList.remove('playing');c.querySelector('.playbtn').textContent='▶';}});
  let a = audios[id];
  if(!a){ a = new Audio(url); audios[id]=a;
    a.addEventListener('ended',()=>{card.classList.remove('playing');card.querySelector('.playbtn').textContent='▶';});
  }
  a.play(); card.classList.add('playing');
  card.querySelector('.playbtn').textContent='❚❚';
  const blind = document.getElementById('blind-'+id);
  if(blind){ blind.remove(); card.classList.add('open'); }
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

if(KEY){
  fetch('/api/auth',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:KEY})})
    .then(r=>{ if(r.ok) enter(); else { localStorage.removeItem('stationKey'); } });
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    mcp.run(transport="http", host="0.0.0.0", port=port)
