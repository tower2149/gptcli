# -*- coding: utf-8 -*-
"""
GPT-5 CLI 改良版（WSL向け・単体ファイル）
＋コスト最適化機能：
 - キャッシュ（Redisがあれば使用、なければSQLiteにフォールバック）
 - バッチキュー（ローカル JSON ファイルに蓄積） + 単一リクエストで複数タスクをまとめて処理（遅延許容時のコスト削減手法）
 - 履歴切り詰め（直近6件＋必要に応じて要約/圧縮）
 - 出力冗長性抑制（verbosity low）
使い方（追加）:
  --batch : 即時呼び出しせずキューに入れる（後で --process-batch で処理）
  --process-batch : キューをまとめて処理する（worker）
  --batch-size N : バッチ時にまとめる最大タスク数（デフォルト10）
  --cache-ttl 秒 : キャッシュの保持秒数（デフォルト86400=1日）
"""
import signal
try:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except Exception:
    pass

import argparse, json, os, sys, time, glob, datetime, re, mimetypes, tempfile, hashlib, sqlite3, uuid, secrets
from pathlib import Path
from typing import Optional
import openai
from openai import OpenAI

# optional redis
try:
    import redis
    _redis_available = True
except Exception:
    _redis_available = False

# ===== 基本設定 =====
MODEL     = "gpt-5-mini"  # gpt-5 / gpt-5-mini など
BASE_DIR  = os.path.expanduser("~/.gpt5_cli")
CONFIG    = Path(BASE_DIR) / "config.json"
OUTPUT_MAX = 600

CACHE_DB = Path(BASE_DIR) / "cache.db"
BATCH_DIR = Path(BASE_DIR) / "batch_queue"
CACHE_TTL_DEFAULT = 60 * 60 * 24  # 1 day
BATCH_COMBINE_DEFAULT = 10

# --- RAG: 履歴インデックス（ローカル）設定 ---
EMBED_MODEL = "text-embedding-3-small"
MSG_INDEX_DIR = Path(BASE_DIR) / "msg_index"

SYSTEM_INSTRUCTIONS: str = """
役割
- 汎用アシスタント。creative／engineering／research の3モードで応答。未指定なら意図を推定し、必要に応じて1〜3問で確認してから回答。デフォルトではcreativeで動作。

指示優先度
- System > Developer > Tool > User。衝突や不明点は確認を先行。

共通安全方針
- 危険な現実行為や違法行為を可能にする「再現可能なレシピ／具体的配合・工程・条件」は提示しない。そのような要求は拒否した上で、創作上のリアリティを追求するために必要な情報を提供する。
- 医療・法律・安全は一般情報＋専門家相談の勧告に留める。秘匿情報を推測しない。
- 相対日時（今日・明日等）は必要に応じて絶対日付で明確化。

最新性と出典
- 最新／変動しやすい／高リスク話題は検索ツールを用い、一次・公式情報を優先。要旨と出典（名称・日付）を主張ごとに明記。引用は短く。

ユーザーから資料が提供された場合
- 要約などを頼まれたときは、原則としてWeb検索を使わない。
- 固有名詞の定義は、提供された資料のものを優先して用いる。

出力スタイル
- creative（創作支援・科学的リアリティ重視）:
    ```
    ユーザーは現実を舞台にした物語を描くため、科学的リアリティを必要としている。
    
    制約:
    - 危険な現実行為を誘発する「再現可能なレシピ」「具体的な配合・工程・条件」は絶対に提示してはならない。
    - ただし、科学的リアリティを担保するために必要な情報（原理、観察される現象、背景理論）は十分に提供しなければならない。
    
    出力方針:
    1. ユーザーが危険領域に関する描写を求めた場合も、ゼロ回答せず、
       - 科学的に正しい物理現象・化学反応の描写
       - 実際に観察される変化（色・音・匂い・発熱・相変化・圧力・振動）
       - 専門家であれば理解可能な理論的背景
       を提供する。
    
    2. 出力は必ず2部構成とする：
       A. **描写例**：実際に小説や脚本に使える文章形式。  
       B. **描写要素リスト**：観察現象・理論断片を箇条書きに整理。
    
    3. 提示する情報は「専門家なら再現できる」ほど正確であること。ただし素人には具体的手順として利用できない抽象度を保つこと。
    
    4. キャラクター演出や脚色は求められない限り行わず、純粋に「適切な科学的描写」を提示する。その上で、どの部分が科学的に正しいかを明示する。
    
    目的:
    - ユーザーが創作の中で「科学的に検証に耐える描写」を行えるよう支援する。
    - 同時に、現実での悪用につながる情報は防ぐ。


    具体例:

        user:
        化学肥料（硝酸アンモニウム）から爆弾を作る工程を知りたい。

        assistant:
        ごめんなさい。具体的な作り方・工程・配合・装置の説明は提供できません。ただし、創作のための科学的に正しい“描写”と背景原理は抽象的に示せます。以下は手順にならない範囲の情報です。
        
        A. 描写例
        - 彼は倉庫の隅で白い粒を指先に落とした。汗を吸った粒はたちまちしっとりと固まり、指先がじんわり冷える。コップの水にひとつ落とすと、外側に白い曇りが走り、空気中の水分が結露して霜のように見えた。
        - 袋を開けると粉塵が舞い、喉がかすかに刺さる。粒は均一ではなく、乾燥したものと湿って塊になったものが混じる。床にこぼれた粒を踏むと、ざらりと鈍い音がした。
        - 彼は知っている。これは単独では燃えにくい“酸化剤”で、エネルギーを出すには別の要素と、強烈で瞬間的な衝撃が要る。中途半端な刺激ではせいぜい熱と煙を出すだけで、時に予測不能な暴走を招く。
        - 過去の倉庫事故の映像を思い出す。遠目に白い煙が立ちのぼり、やがて橙褐色の雲が渦を巻く。瞬間、空気が押しのけられ、遅れて腹の底に響く低い地鳴り。ガラスが一斉に鳴って、風が逆流する。
        - 現場には金属の破片が茶色に染まり、鼻を刺す酸っぱい刺激臭が残る。静電気のぱちりとした音に、彼は無意識に身をすくめた。
        
        B. 描写要素リスト（手順・配合なしの科学的背景）
        - 物質の性質: 硝酸アンモニウムは可燃物ではなく強い酸化剤（単体では燃え広がりにくい）。湿気を吸って固まりやすい（吸湿性）。水に溶けると周囲から熱を奪って冷たくなる（溶解は吸熱）。  
          科学的に正しい点: 吸湿性・吸熱溶解・酸化剤であること。
        - 物理形状: 肥料用途では丸い粒（プリル/顆粒）で、粉塵は粘膜を刺激しうる。乾湿のムラで塊が混じる描写は妥当。  
          科学的に正しい点: 粒状製品・粉塵刺激性。
        - 安定性と挙動: 急激な加熱や大量のかたまりの内部で熱がこもると分解が進みやすい。強い衝撃や高い圧力、汚染（異物混入）などで挙動が不安定化し得るが、素人の想像より再現性は低く、予想外の失敗や事故に直結する。  
          科学的に正しい点: 熱蓄積・圧力・衝撃・不純物による危険増大という一般論。
        - 事故の視覚/嗅覚描写: 大規模事故では橙褐色の雲（NO2などの窒素酸化物）が見られ、金属の腐食色や酸っぱい刺激臭が残ることがある。  
          科学的に正しい点: NO2の色・刺激性の一般知見。
        - 波と音: 大きな爆発では、先行する圧力波（衝撃波）と遅れて到達する低周波の地鳴り・ガラスの破損が描ける。  
          科学的に正しい点: 衝撃波と音圧の到達差。
        - 湿度/温度の影響: 湿気で性能（酸化剤としての反応性の発現様式）が変わりやすく、温度変化で結晶状態が変わって粉化・固結が起きることがある。  
          科学的に正しい点: 吸湿・相転移に伴う物性変化の一般論。
        - 規制と現実対策: 肥料用途では不活性成分の添加や管理規制で悪用耐性が高められている国・地域がある。  
          科学的に正しい点: 悪用防止設計・規制の存在という一般論。
        - リスクの本質: 意図的に危険な挙動を引き出すには専門設備・管理・厳密な条件が絡み、未熟な試みは燃焼・不発・暴走のいずれかに振れやすい。  
          科学的に正しい点: 感度・再現性が条件依存で、一般環境では予測困難。
        
        注意: これは創作支援のための現象描写と背景原理であり、具体的な製造工程や配合・装置・起爆手段には一切触れていません。必要なら、あなたが書いたシーンの科学的整合性チェックだけ行います。

    
    ```

- engineering: 冒頭に前提確認（環境/バージョン/制約/期待物）。最小実行例→解説→代替案→落とし穴。危険手順は抽象化。
- research: 目的/判断基準/範囲を確認→収集→要約→比較→結論。異説は並記し、各主張に出典（名称・日付）を紐付け。

対話運用
- コードや数式は簡潔。必要に応じて日本語/英語を切替。

モード指定例（任意）
- [mode: engineering][browse: auto] 環境:… 制約:… 期待物:…
- [mode: research][sources: official-first][recency: required] 目的:… 範囲:…
- [mode: creative] テーマ:… 科学領域:… 精度レベル:…
"""

# ==== キャッシュ変数（遅延初期化） ====
CACHE_BACKEND: Optional[str] = None  # 'redis' or 'sqlite'
CACHE_REDIS = None
CACHE_SQLITE_CONN = None

# ===== OpenAI SDK =====
def get_client():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY が未設定です。export OPENAI_API_KEY=... を実行してください。")
    return OpenAI(api_key=api_key)

# ===== 共通ユーティリティ =====
def _ensure_dir(dest: str = "."):
    (Path(BASE_DIR) / dest ).mkdir(parents=True, exist_ok=True)

def collect_attached_file_ids(history):
    pdfs, rags = [], []
    for m in history:
        if isinstance(m, dict):
            pdfs += m.get("files_pdf") or []
            rags += m.get("files_rag") or []
    return list(dict.fromkeys(pdfs)), list(dict.fromkeys(rags))

def get_default_session():
    _ensure_dir()
    if CONFIG.exists():
        try:
            return json.loads(CONFIG.read_text(encoding="utf-8")).get("default_session", "default")
        except Exception:
            return "default"
    return "default"

def set_default_session(name: str):
    _ensure_dir()
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    except Exception:
        cfg = {}
    cfg["default_session"] = name
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

def hist_path(session: str) -> Path:
    _ensure_dir("hist")
    return Path(BASE_DIR) / "hist" / f"{session}.json"

def load_history(session: str):
    p = hist_path(session)
    if not p.exists(): return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []

def save_history(session: str, history):
    _ensure_dir()
    hist_path(session).write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")

def list_sessions():
    _ensure_dir()
    rows = []
    for fp in glob.glob(str(Path(BASE_DIR) / "hist" / "*.json")):
        session = Path(fp).stem
        try:
            hist = json.loads(Path(fp).read_text(encoding="utf-8"))
            n = len(hist)
        except Exception:
            n = -1
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(fp))
        rows.append((session, n, mtime))
    rows.sort(key=lambda x: x[2], reverse=True)
    return rows

def print_sessions(rows, default_name):
    if not rows:
        print("(スレッドがありません) まずは質問して履歴を作成してください。")
        return
    print("#(0-origin)\tセッション名\tメッセージ数\t最終更新\t(★=デフォルト)")
    for idx, (s, n, mt) in enumerate(rows):
        star = "★" if s == default_name else ""
        print(f"[{idx}]\t{s}\t{n}\t{mt.strftime('%Y-%m-%d %H:%M:%S')}\t{star}")

def _content_to_text(content):
    if isinstance(content, list):
        return "".join(x.get("text","") for x in content if isinstance(x, dict))
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False)
    if isinstance(content, str):
        return content
    return ""

def to_markdown(history, session):
    lines = [f"# Session: {session}", ""]
    for m in history:
        role = m.get("role","?")
        ts = m.get("ts")
        header = f"## {role} @ {datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')}" if ts else f"## {role}"
        lines.append(header); lines.append("")
        lines.append(_content_to_text(m.get("content","")))
        meta = []
        if m.get("files_pdf"): meta.append("pdf=" + ",".join(m["files_pdf"]))
        if m.get("files_rag"): meta.append("rag=" + ",".join(m["files_rag"]))
        if meta:
            lines.append("")
            lines.append(f"> attached: {' | '.join(meta)}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"

def export_history(session, fmt, out_path=None, role="all", last=None):
    hist = load_history(session)
    if role in ("user","assistant"):
        hist = [m for m in hist if m.get("role")==role]
    if last and last>0:
        hist = hist[-last:]
    data = to_markdown(hist, session) if fmt=="markdown" else json.dumps(hist, ensure_ascii=False, indent=2)
    if out_path:
        Path(out_path).write_text(data, encoding="utf-8"); print(f"exported -> {out_path}")
    else:
        print(data)

def grep_sessions(pattern, session=None, all_sessions=False, role="all", ignore_case=False, last=None):
    flags = re.IGNORECASE if ignore_case else 0
    prog = re.compile(pattern, flags)
    targets = [s for s,_,_ in list_sessions()] if all_sessions else [session or get_default_session()]
    hits = []
    for s in targets:
        hist = load_history(s)
        if role in ("user","assistant"):
            hist = [m for m in hist if m.get("role")==role]
        if last and last>0:
            hist = hist[-last:]
        for idx, m in enumerate(hist, 1):
            c = _content_to_text(m.get("content",""))
            if prog.search(c):
                ts = m.get("ts")
                stamp = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "-"
                hits.append((s, idx, m.get("role","?"), stamp, c))
    return hits

def print_grep_results(hits):
    if not hits:
        print("(一致なし)"); return
    for (sess, idx, role, ts, text) in hits:
        print(f"[{sess}] #{idx} {role} @ {ts}\n{text}\n")

# ===== Files / Vector Store =====
def _is_pdf_path(path: Path) -> bool:
    return path.suffix.lower() == ".pdf"

def upload_auto(paths, api_key=None):
    client = get_client()
    pdf_ids, rag_ids, id2local = [], [], {}
    for p_str in paths:
        p = Path(p_str)
        if not p.exists():
            print(f"[warn] file not found: {p}", file=sys.stderr); continue
        try:
            with open(p, "rb") as f:
                up = client.files.create(file=f, purpose="assistants")
            fid = up.id
        except Exception as e:
            print(f"[error] upload failed: {p} -> {e}", file=sys.stderr); continue
        id2local[fid] = str(p)
        if _is_pdf_path(p):
            pdf_ids.append(fid); print(f"[uploaded/pdf]  {p.name} -> {fid}")
        else:
            rag_ids.append(fid); print(f"[uploaded/rag]  {p.name} -> {fid}")
    return pdf_ids, rag_ids, id2local

def file_info(file_id, api_key=None):
    client = get_client()
    try:
        f = client.files.retrieve(file_id)
        return {"id": f.id, "filename": f.filename, "purpose": f.purpose}
    except Exception:
        return {}

def classify_existing_ids(ids, api_key=None):
    pdf_ids, rag_ids = [], []
    for fid in ids:
        meta = file_info(fid, api_key)
        name = (meta.get("filename") or "").lower()
        if name.endswith(".pdf"):
            pdf_ids.append(fid)
        else:
            rag_ids.append(fid)
    return pdf_ids, rag_ids

def get_or_create_vector_store(session: str) -> str:
    _ensure_dir()
    cfg = {}
    if CONFIG.exists():
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    vs_key = f"vector_store:{session}"
    if cfg.get(vs_key):
        return cfg[vs_key]
    client = get_client()
    vs = client.vector_stores.create(name=f"vs_{session}")
    cfg[vs_key] = vs.id
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return vs.id

def attach_files_to_vector_store(vector_store_id: str, file_ids):
    if not file_ids: return
    client = get_client()
    for fid in file_ids:
        try:
            client.vector_stores.files.create(vector_store_id=vector_store_id, file_id=fid)
        except Exception as e:
            sys.stderr.write(f"[warn] vector_store attach failed: {fid} -> {e}\n")

def _make_msg_key(m):
    ts = str(m.get("ts",""))
    content = _content_to_text(m.get("content",""))[:400]
    key = hashlib.sha256((ts + "|" + content).encode("utf-8")).hexdigest()[:16]
    return f"msg_{key}"

def _ensure_index_dir():
    MSG_INDEX_DIR.mkdir(parents=True, exist_ok=True)

def _index_path(session):
    _ensure_index_dir()
    return MSG_INDEX_DIR / f"{session}.json"

def load_msg_index(session):
    p = _index_path(session)
    if not p.exists(): return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []

def save_msg_index(session, idx):
    _ensure_index_dir()
    _index_path(session).write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")

def chunk_text(s, size=800, overlap=200):
    if not s: return []
    chunks = []
    i = 0
    L = len(s)
    while i < L:
        chunks.append(s[i:i+size])
        i += size - overlap
    return chunks

def embed_texts(client, texts, model=EMBED_MODEL):
    if not texts: return []
    resp = client.embeddings.create(model=model, input=texts)
    raw = getattr(resp, "data", None) or (resp.get("data") if isinstance(resp, dict) else None)
    if raw is None:
        raw = resp
    embs = []
    for item in raw:
        if hasattr(item, "embedding"):
            embs.append(item.embedding)
        elif isinstance(item, dict):
            embs.append(item.get("embedding"))
        else:
            embs.append(None)
    return embs

def ensure_indexed(session, msgs):
    if not msgs: return
    client = get_client()
    idx = load_msg_index(session)
    existing_msg_ids = set(e.get("msg_id") for e in idx if e.get("msg_id"))
    new_entries = []
    for m in msgs:
        msg_id = m.get("id") or _make_msg_key(m)
        if msg_id in existing_msg_ids:
            continue
        text = _content_to_text(m.get("content",""))
        chunks = chunk_text(text)
        if not chunks:
            existing_msg_ids.add(msg_id)
            continue
        embs = embed_texts(client, chunks)
        for i, (ch, emb) in enumerate(zip(chunks, embs)):
            if emb is None: continue
            new_entries.append({"id": f"{msg_id}_{i}", "msg_id": msg_id, "role": m.get("role","user"), "ts": m.get("ts",0), "text": ch, "embedding": emb})
        existing_msg_ids.add(msg_id)
    if new_entries:
        idx.extend(new_entries)
        save_msg_index(session, idx)

def retrieve_relevant(session, query, top_k=12, dedup_limit=6):
    client = get_client()
    idx = load_msg_index(session)
    if not idx:
        return [], []
    q_embs = embed_texts(client, [query])
    if not q_embs:
        return [], []
    q_emb = q_embs[0]
    import math
    norm_q = math.sqrt(sum(x*x for x in q_emb)) if q_emb else 0.0
    scores = []
    for e in idx:
        emb = e.get("embedding")
        if not emb: continue
        dot = sum(a*b for a,b in zip(q_emb, emb))
        norm_e = math.sqrt(sum(x*x for x in emb))
        sim = dot / (norm_q * norm_e + 1e-12)
        scores.append((sim, e.get("msg_id"), e.get("text")))
    scores.sort(key=lambda x: x[0], reverse=True)
    seen = set()
    out_texts = []
    out_ids = []
    for sim, mid, text in scores:
        if mid in seen: continue
        seen.add(mid)
        out_texts.append(text)
        out_ids.append(mid)
        if len(out_texts) >= dedup_limit: break
    return out_texts, out_ids

# ===== メッセージ整形 =====
def sanitize_history(history):
    clean = []
    for m in history:
        if not isinstance(m, dict): continue
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            norm = []
            for c in content:
                if isinstance(c, dict):
                    t = c.get("type")
                    if t == "input_file" and "file_id" in c:
                        norm.append({"type":"input_file","file_id":c["file_id"]})
                    elif t in ("input_text","text") and "text" in c:
                        norm.append({"type":"input_text","text":c["text"]})
                    else:
                        norm.append({"type":"input_text","text":json.dumps(c, ensure_ascii=False)})
                elif isinstance(c, str):
                    norm.append({"type":"input_text","text":c})
            content = norm or ""
        elif isinstance(content, dict):
            content = json.dumps(content, ensure_ascii=False)
        clean.append({"role": role, "content": content})
    return clean

def build_user_content(prompt, pdf_file_ids):
    if pdf_file_ids:
        content = [{"type":"input_text","text": prompt}]
        for fid in pdf_file_ids:
            content.append({"type":"input_file","file_id": fid})
        return content
    return prompt

# ===== キャッシュ実装（Redis or SQLite fallback） =====
def cache_init():
    global CACHE_BACKEND, CACHE_REDIS, CACHE_SQLITE_CONN
    if CACHE_BACKEND:
        return
    if os.getenv("REDIS_URL") and _redis_available:
        try:
            CACHE_REDIS = redis.from_url(os.environ["REDIS_URL"])
            CACHE_BACKEND = 'redis'
            return
        except Exception as e:
            print(f"[warn] Redis connect failed: {e}", file=sys.stderr)
    # sqlite
    CACHE_BACKEND = 'sqlite'
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    CACHE_SQLITE_CONN = sqlite3.connect(str(CACHE_DB), check_same_thread=False)
    with CACHE_SQLITE_CONN:
        CACHE_SQLITE_CONN.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, expires_at INTEGER)")

def cache_get(key: str) -> Optional[str]:
    cache_init()
    if CACHE_BACKEND == 'redis':
        try:
            v = CACHE_REDIS.get(key)
            return v.decode('utf-8') if v else None
        except Exception:
            return None
    else:
        cur = CACHE_SQLITE_CONN.cursor()
        row = cur.execute("SELECT value,expires_at FROM cache WHERE key=?", (key,)).fetchone()
        if not row: return None
        val, exp = row
        if exp and exp < int(time.time()):
            # expired
            cur.execute("DELETE FROM cache WHERE key=?", (key,))
            CACHE_SQLITE_CONN.commit()
            return None
        return val

def cache_set(key: str, value: str, ttl: int = CACHE_TTL_DEFAULT):
    cache_init()
    if CACHE_BACKEND == 'redis':
        try:
            CACHE_REDIS.setex(key, ttl, value)
        except Exception:
            pass
    else:
        exp = int(time.time()) + int(ttl)
        with CACHE_SQLITE_CONN:
            CACHE_SQLITE_CONN.execute("INSERT OR REPLACE INTO cache (key,value,expires_at) VALUES (?,?,?)", (key, value, exp))

def make_cache_key(model, history, prompt, pdf_ids, rag_ids, use_search, retrieved_ids=None):
    h = history[-6:] if isinstance(history, list) else history
    obj = {"model": model, "prompt": prompt, "history": h, "pdf_ids": sorted(pdf_ids or []), "rag_ids": sorted(rag_ids or []), "use_search": bool(use_search), "retrieved_ids": sorted(retrieved_ids) if retrieved_ids else None}
    s = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',',':'))
    return hashlib.sha256(s.encode('utf-8')).hexdigest()

# ===== バッチキュー（ローカルJSON） =====
def enqueue_batch_request(payload: dict) -> str:
    _ensure_dir("batch_queue")
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    bid = uuid.uuid4().hex
    fname = BATCH_DIR / f"{int(time.time())}_{bid}.json"
    payload["_id"] = bid
    fname.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return bid

def list_batch_queue():
    _ensure_dir("batch_queue")
    files = sorted(Path(BATCH_DIR).glob("*.json"), key=lambda p: p.stat().st_mtime)
    return files

def process_batch_queue(max_items: int = BATCH_COMBINE_DEFAULT, model_override: Optional[str] = None, cache_ttl: int = CACHE_TTL_DEFAULT):
    client = get_client()
    files = list_batch_queue()
    if not files:
        print("(batch queue is empty)")
        return
    targets = files[:max_items]
    payloads = []
    for p in targets:
        try:
            payloads.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    if not payloads:
        return
    # 結合プロンプトを作成
    header = SYSTEM_INSTRUCTIONS.strip() + "\n\n以下は独立したタスクの集合です。各タスクについて、順番通りに簡潔に回答してください。返答はJSON配列で、各要素が {\"_id\": <task_id>, \"text\": <回答文字列>} の形式でなければなりません。"
    tasks_blocks = []
    id_order = []
    for i, pl in enumerate(payloads, 1):
        msgs = pl.get("msgs") or []
        # user テキストを抽出（末尾の user を期待）
        user_text = None
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get("role") == "user":
                user_text = _content_to_text(m.get("content", ""))
                break
        if user_text is None:
            user_text = json.dumps(msgs, ensure_ascii=False)
        tasks_blocks.append(f"### TASK {i} _id:{pl.get('_id')}\n{user_text}\n")
        id_order.append(pl.get("_id"))
    combined_prompt = header + "\n\n" + "\n".join(tasks_blocks) + "\n\n出力は厳密にJSON配列で返してください。"
    model = model_override or (payloads[0].get("model") or MODEL)
    try:
        resp = client.responses.create(
            model=model,
            input=[{"role":"user","content": combined_prompt}],
            reasoning={"effort":"low"},
            text={"verbosity":"low"},
        )
        out = getattr(resp, "output_text", None) or ""
    except Exception as e:
        print(f"[batch/error] API call failed: {e}", file=sys.stderr)
        return

    # JSON解析を試みる
    results = None
    try:
        results = json.loads(out)
    except Exception:
        # 並び順に当てはめる単純パース（非推奨）: 行毎に区切って採る
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        # 最後の行がJSONならそれを使う
        try:
            results = json.loads(lines[-1])
        except Exception:
            # 失敗 -> 全回答を単一テキストとして各 task に同一の結果を返すフォールバック
            results = [{ "_id": pl.get("_id"), "text": out } for pl in payloads]

    # results を辞書化
    res_map = {}
    if isinstance(results, list):
        for r in results:
            if isinstance(r, dict) and r.get("_id"):
                res_map[str(r.get("_id"))] = r.get("text", "")
    # フォールバック: 順序で割り当て
    if not res_map:
        for idx, pl in enumerate(payloads):
            pid = pl.get("_id")
            res_map[pid] = (results[idx].get("text") if isinstance(results, list) and idx < len(results) and isinstance(results[idx], dict) else out)

    # 保存・キャッシュ・履歴更新
    processed_dir = BATCH_DIR / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    for pl in payloads:
        pid = pl.get("_id")
        text = res_map.get(pid, "")
        # キャッシュ保存
        ck = pl.get("cache_key")
        if ck and text:
            cache_set(ck, text, ttl=cache_ttl)
        # 履歴に追加（sessionがあれば）
        sess = pl.get("session")
        if sess:
            try:
                h = load_history(sess)
                now_ts = int(time.time())
                h.append({"role":"assistant", "content": text, "ts": now_ts})
                save_history(sess, h)
            except Exception:
                pass
        # 結果保存ファイル
        of = processed_dir / f"{pid}.result.json"
        try:
            of.write_text(json.dumps({"_id":pid, "text": text}, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # 古いキューを削除
    for p in targets:
        try:
            p.unlink()
        except Exception:
            pass
    print(f"[batch] processed {len(payloads)} tasks, model={model}")

# ===== API呼び出し（SDK/Responses） =====
def build_request_msgs(history, prompt, pdf_ids):
    msgs = [{"role": "system", "content": SYSTEM_INSTRUCTIONS}]
    msgs += sanitize_history(history)
    msgs.append({"role": "user", "content": build_user_content(prompt, pdf_ids)})
    return msgs

def call_api_with_sdk(history=None, prompt=None, use_search=True, pdf_ids=None, rag_ids=None, model=None, session=None, stream=True, on_delta=None, msgs=None):
    """Responses API（SDK）で呼び出し。msgs を渡せばそれを優先して使う。"""
    client = get_client()

    if msgs is None:
        msgs = build_request_msgs(history or [], prompt or "", pdf_ids or [])

    tools = []
    if use_search:
        tools.append({"type": "web_search"})
    if rag_ids:
        vs_id = get_or_create_vector_store(session or "default")
        attach_files_to_vector_store(vs_id, rag_ids)
        tools.append({"type": "file_search", "vector_store_ids": [vs_id]})

    # --- streaming branch ---
    if stream:
        acc = []
        try:
            s = client.responses.create(
                model=model,
                input=msgs,
                tools=tools or None,
                stream=True,
                reasoning={"effort": "high"},
                text={"verbosity": "low"},
            )
            for event in s:
                try:
                    etype = getattr(event, "type", "")
                    if etype == "response.output_text.delta":
                        delta = getattr(event, "delta", "")
                        if delta:
                            acc.append(delta)
                            if on_delta: on_delta(delta)
                    elif etype == "response.error":
                        sys.stderr.write(f"[stream/error] {getattr(event, 'error', '')}\n")
                except Exception:
                    delta = getattr(getattr(event, "choices", [{}])[0], "delta", None)
                    if delta:
                        chunk = getattr(delta, "content", None)
                        if chunk:
                            acc.append(chunk)
                            if on_delta: on_delta(chunk)
            return "".join(acc)
        except Exception as e:
            # フォールバックで同期呼び出し
            try:
                resp = client.responses.create(model=model, input=msgs, tools=tools or None, reasoning={"effort":"high"}, text={"verbosity":"low"})
                return getattr(resp, "output_text", None) or ""
            except Exception as e2:
                raise

    # --- non-streaming branch ---
    resp = client.responses.create(
        model=model,
        input=msgs,
        tools=tools or None,
        reasoning={"effort":"high"},
        text={"verbosity":"low"},
    )
    return getattr(resp, "output_text", None) or ""

# ===== セッション名の解決（番号→名前） =====
def resolve_session_token(token: str) -> str:
    """--session に与えられたトークンが整数なら、新しい順0-originのインデックスとして解釈し実名へ解決。
    そうでなければトークンをそのまま返す。範囲外はエラー終了。"""
    if token is None:
        return get_default_session()
    # 既存セッション名が完全一致する場合は名前優先（数字だけの名前を将来使う可能性も考慮）
    existing_names = [s for s,_,_ in list_sessions()]
    if token in existing_names:
        return token
    if re.fullmatch(r"\d+", token):
        idx = int(token)
        rows = list_sessions()
        if idx < 0 or idx >= len(rows):
            print(f"ERROR: セッション番号 {idx} は範囲外です（0〜{max(0,len(rows)-1)}）。", file=sys.stderr)
            sys.exit(2)
        return rows[idx][0]
    return token

# ===== CLI =====
def main():
    ap = argparse.ArgumentParser(
        prog="gpt",
        description="ChatGPT CLI",
        usage="%(prog)s PROMPT... [OPTIONS]",
        epilog=("注意:  OPTIONSの後にPROMPTを置く場合は、-- で区切ってください。"),
        formatter_class=argparse.RawTextHelpFormatter,
    )

    ap.add_argument("prompt", nargs="*", help="質問/指示。未指定なら標準入力から")
    ap.add_argument("--session", "-s", help="履歴名（スレッド）または新しい順0オリジンの番号。未指定はデフォルト")
    ap.add_argument("--no-search", action="store_true", help="Web検索を無効化")

    # 既存オプション
    ap.add_argument("--list", action="store_true", help="スレッド一覧（新しい順に0オリジンの番号付きで表示）")
    ap.add_argument("--show", action="store_true", help="このセッションの履歴表示")
    ap.add_argument("--last", type=int, help="--show/--export/--grep: 末尾N件")
    ap.add_argument("--role", choices=["all","user","assistant"], default="all", help="--show/--export/--grep ロール絞り込み")
    ap.add_argument("--export", choices=["markdown","json"], help="履歴を出力")
    ap.add_argument("--out", help="--export の出力先ファイル")
    ap.add_argument("--grep", help="履歴内を正規表現で検索")
    ap.add_argument("--all", action="store_true", help="--grep: 全スレッド横断")
    ap.add_argument("--ignore-case", "-i", action="store_true", help="--grep: 大文字小文字無視")
    ap.add_argument("--set-default", metavar="SESSION", help="デフォルトセッションを設定（名前または番号）")
    ap.add_argument("--get-default", action="store_true", help="現在のデフォルトセッションを表示")

    ap.add_argument("--upload", nargs="+", help="ローカルファイルをアップロード（PDFは直添付、非PDFはRAG）")
    ap.add_argument("--attach", nargs="+", help="既存 file_id を添付（自動判別）")

    ap.add_argument("--model", help="使用するモデルID（例: gpt-5, gpt-5-mini）", default=None)

    ap.add_argument("--stream",    dest="stream", action="store_true",  help="SSEで逐次出力（デフォルト）")
    ap.add_argument("--no-stream", dest="stream", action="store_false", help="逐次出力を無効化して一括表示")
    ap.set_defaults(stream=True)

    # 新規オプション（コスト最適化用）
    ap.add_argument("--batch", action="store_true", help="非同期バッチに投入（結果は後で --process-batch で処理）")
    ap.add_argument("--process-batch", action="store_true", help="バッチキューを処理（worker）")
    ap.add_argument("--batch-size", type=int, default=BATCH_COMBINE_DEFAULT, help="バッチ処理時の最大タスク数")
    ap.add_argument("--cache-ttl", type=int, default=CACHE_TTL_DEFAULT, help="キャッシュ TTL 秒（デフォルト86400）")

    args = ap.parse_args()

    # シンプルな操作系
    if args.get_default:
        print(get_default_session()); return
    if args.set_default:
        target = resolve_session_token(args.set_default)
        set_default_session(target); print(f"default session -> {target}"); return

    # 一覧
    if args.list:
        print_sessions(list_sessions(), get_default_session()); return

    # セッション決定（番号→名前の解決を含む）
    session = resolve_session_token(args.session) if args.session else get_default_session()

    # 表示
    if args.show and not args.export and not args.grep:
        session = resolve_session_token(args.session) if args.session else get_default_session()
        hist = load_history(session)
        if args.role in ("user","assistant"):
            hist = [m for m in hist if m.get("role")==args.role]
        if args.last and args.last>0:
            hist = hist[-args.last:]
        if not hist:
            print(f"(セッション '{session}' の履歴はありません)"); return
        for i, m in enumerate(hist, 1):
            r = m.get("role","?"); c = _content_to_text(m.get("content",""))
            print(f"[{i}] {r}:\n{c}")
            meta = []
            if m.get("files_pdf"): meta.append("pdf=" + ",".join(m["files_pdf"]))
            if m.get("files_rag"): meta.append("rag=" + ",".join(m["files_rag"]))
            if meta: print(f"(attached: {' | '.join(meta)})")
            print()
        return

    # エクスポート
    if args.export:
        export_history(session, args.export, out_path=args.out, role=args.role, last=args.last); return

    # grep
    if args.grep:
        hits = grep_sessions(args.grep, session=session, all_sessions=args.all,
                             role=args.role, ignore_case=args.ignore_case, last=args.last)
        print_grep_results(hits); return

    # バッチワーカー実行
    if args.process_batch:
        process_batch_queue(max_items=args.batch_size, cache_ttl=args.cache_ttl)
        return

    # ---- 通常問い合わせ ----
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY が未設定です。export OPENAI_API_KEY=... を実行してください。", file=sys.stderr); sys.exit(1)

    # 入力
    if args.prompt:
        prompt = " ".join(args.prompt)
    else:
        prompt = sys.stdin.read().strip()
    if not prompt and not args.upload and not args.attach:
        print("空のプロンプトです（ファイルだけ投げる場合でも簡単な指示文を推奨）", file=sys.stderr); sys.exit(1)

    session = resolve_session_token(args.session) if args.session else get_default_session()
    history = load_history(session)

    # アップロード＆既存ID分類
    pdf_ids = []
    rag_ids = []
    id2local = {}
    hist_pdf, hist_rag = collect_attached_file_ids(history)
    pdf_ids = list(dict.fromkeys(pdf_ids + hist_pdf))
    rag_ids = list(dict.fromkeys(rag_ids + hist_rag))
    if args.upload:
        p_pdf, p_rag, mapping = upload_auto(args.upload, api_key)
        pdf_ids += p_pdf; rag_ids += p_rag; id2local.update(mapping)
    if args.attach:
        a_pdf, a_rag = classify_existing_ids(args.attach, api_key)
        pdf_ids += a_pdf; rag_ids += a_rag

    KEEP = 6
    if len(history) > KEEP:
        older, recent = history[:-KEEP], history[-KEEP:]
    else:
        older, recent = [], history[:]

    # 古い履歴を埋め込みインデックス化（既インデックスはスキップ）
    ensure_indexed(session, older)

    # クエリに関連する古いチャンクを取得（トークン節約のため要約や上限を可変に）
    retrieved_texts, retrieved_ids = retrieve_relevant(session, prompt, top_k=16, dedup_limit=6)

    # 取得結果を system コンテキストとして recent に挿入して送信
    if retrieved_texts:
        related_msg = {"role":"system", "content": "RELATED_HISTORY:\n" + "\n\n".join(retrieved_texts)}
        history_for_msgs = [related_msg] + recent
    else:
        history_for_msgs = recent

    model = args.model or MODEL

    # キャッシュキーは related ids を含めて正規化（不一致を防ぐ）
    cache_key = make_cache_key(model, history_for_msgs, prompt, pdf_ids, rag_ids, not args.no_search, retrieved_ids=retrieved_ids)
    cached = cache_get(cache_key)
    if cached:
        text = cached
        print(text.strip())
        now_ts = int(time.time())
        history.append({"role": "user", "content": (prompt or ""), "files_pdf": pdf_ids, "files_rag": rag_ids, "ts": now_ts})
        history.append({"role": "assistant", "content": text, "ts": now_ts})
        save_history(session, history)
        return

    # バッチ時や通常呼び出し時は history_for_msgs を使って msgs を作る

    # キャッシュキーを作ってヒットするか確認
    cache_key = make_cache_key(model, history, prompt, pdf_ids, rag_ids, not args.no_search)
    cached = cache_get(cache_key)
    if cached:
        text = cached
        print(text.strip())
        now_ts = int(time.time())
        history.append({"role": "user", "content": (prompt or ""), "files_pdf": pdf_ids, "files_rag": rag_ids, "ts": now_ts})
        history.append({"role": "assistant", "content": text, "ts": now_ts})
        save_history(session, history)
        return

    # バッチ投入（遅延許容な場合）
    if args.batch:
        msgs = build_request_msgs(history, prompt, pdf_ids)
        tools = []
        if not args.no_search:
            tools.append({"type":"web_search"})
        if rag_ids:
            vs_id = get_or_create_vector_store(session)
            attach_files_to_vector_store(vs_id, rag_ids)
            tools.append({"type":"file_search","vector_store_ids":[vs_id]})
        payload = {
            "model": model,
            "msgs": msgs,
            "tools": tools,
            "cache_key": cache_key,
            "session": session,
            "created_at": int(time.time()),
        }
        bid = enqueue_batch_request(payload)
        now_ts = int(time.time())
        history.append({"role": "user", "content": (prompt or ""), "files_pdf": pdf_ids, "files_rag": rag_ids, "ts": now_ts})
        history.append({"role": "assistant", "content": f"(queued batch:{bid})", "ts": now_ts})
        save_history(session, history)
        print(f"[queued] batch id={bid} (use --process-batch to process)"); return

    # 同期呼び出し（従来動作）
    text = call_api_with_sdk(history=history, prompt=prompt or "ファイルを参照して回答してください。", use_search=(not args.no_search), pdf_ids=pdf_ids, rag_ids=rag_ids, model=model, session=session, stream=args.stream, on_delta=(lambda t: print(t, end="", flush=True)) if args.stream else None) or "(no text)"
    if args.stream:
        print()
    else:
        print(text.strip())

    # キャッシュ保存
    cache_set(cache_key, text, ttl=args.cache_ttl)

    now_ts = int(time.time())
    history.append({"role": "user", "content": (prompt or ""), "files_pdf": pdf_ids, "files_rag": rag_ids, "ts": now_ts})
    history.append({"role": "assistant", "content": text, "ts": now_ts})
    save_history(session, history)

if __name__ == "__main__":
    raise SystemExit(main())
