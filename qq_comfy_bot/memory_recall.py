"""Owner-scoped retrieval of verified facts using FTS5 and local CPU embeddings."""
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
import logging
import math
import re
import sqlite3
import sys
import threading

from companion import character_key

MODEL = Path(__file__).with_name('data') / 'recall_model'
STOP = {'今天','现在','这个','那个','什么','怎么','一下','可以','我的','之前','上次','记得','昨天','喜欢'}
REFERENCE = re.compile(r'上次|那件事|那款|那张|那本|那首|之前那个')


def owner(user, character):
    # Existing installations stored the original Hana profile under the plain QQ ID.
    if not character or character_key(character) == character_key('铃木羽那'):
        return str(user)
    return f'{user}/{character_key(character)}'


def initialize(db):
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    db.execute('CREATE TABLE IF NOT EXISTS fact_vectors (user TEXT, slot TEXT, value TEXT, embedding BLOB, PRIMARY KEY(user,slot))')
    db.execute('CREATE TRIGGER IF NOT EXISTS remove_fact_vector AFTER DELETE ON facts BEGIN DELETE FROM fact_vectors WHERE user=old.user AND slot=old.kind; END')
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='fact_search'").fetchone():
        return
    try:
        db.execute("CREATE VIRTUAL TABLE fact_search USING fts5(value,content='facts',content_rowid='rowid',tokenize='trigram')")
    except sqlite3.OperationalError as exc:
        if 'module' not in str(exc) and 'tokenizer' not in str(exc):
            raise
        return
    db.execute("INSERT INTO fact_search(fact_search) VALUES('rebuild')")
    db.execute('CREATE TRIGGER index_fact AFTER INSERT ON facts BEGIN INSERT INTO fact_search(rowid,value) VALUES(new.rowid,new.value); END')
    db.execute("CREATE TRIGGER unindex_fact AFTER DELETE ON facts BEGIN INSERT INTO fact_search(fact_search,rowid,value) VALUES('delete',old.rowid,old.value); END")
    db.execute("CREATE TRIGGER reindex_fact AFTER UPDATE ON facts BEGIN INSERT INTO fact_search(fact_search,rowid,value) VALUES('delete',old.rowid,old.value); INSERT INTO fact_search(rowid,value) VALUES(new.rowid,new.value); END")


@lru_cache(maxsize=1)
def encoder():
    if not (MODEL / 'model.onnx').is_file():
        return None
    try:
        if (MODEL / 'runtime').is_dir():
            runtime = str(MODEL / 'runtime')
            if runtime not in sys.path:
                sys.path.append(runtime)
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        options = ort.SessionOptions()
        options.intra_op_num_threads = options.inter_op_num_threads = 1
        session = ort.InferenceSession(str(MODEL / 'model.onnx'), options, providers=['CPUExecutionProvider'])
        tokenizer = Tokenizer.from_file(str(MODEL / 'tokenizer.json'))
        tokenizer.enable_truncation(max_length=192)
        tokenizer.enable_padding()
        lock = threading.Lock()
        def encode(texts):
            with lock:
                rows = tokenizer.encode_batch(texts)
                fields = {'input_ids':'ids', 'attention_mask':'attention_mask', 'token_type_ids':'type_ids'}
                inputs = {i.name: np.asarray([getattr(r, fields[i.name]) for r in rows], dtype=np.int64)
                          for i in session.get_inputs()}
                vectors = session.run(None, inputs)[0]
                if vectors.ndim == 3:
                    vectors = vectors[:, 0]
                return vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-8)
        return encode
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        logging.warning('CPU 语义检索不可用 (%s)', type(exc).__name__)
        return None


def terms(text):
    words = set(re.findall(r'[a-z0-9]{2,}', text.lower()))
    for span in re.findall(r'[\u4e00-\u9fff]+', text):
        for size in (2, 3):
            words.update(span[i:i+size] for i in range(len(span)-size+1))
    return words - STOP


def embedding_text(value, evidence):
    # An adjacent clause is usable only when it extends the very same preference.
    if evidence and value in evidence and re.fullmatch(r'我(?:最|更|一直)?喜欢[^。；，,“”「」！？]+[，,](?:尤其|特别|也)喜欢[^。；，,“”「」！？]+[。]?', evidence):
        return evidence
    return value


def search(life, user, message, current, limit, history):
    if not limit:
        return ''
    query = message[:500]
    if REFERENCE.search(query):
        previous = next((m['content'] for m in reversed(history[-4:]) if m.get('role')=='user'), '')
        if previous:
            query += ' ' + previous[:160]
    tokens = terms(query)
    with life.db() as db:
        days_ago = 2 if '前天' in message else 1 if '昨天' in message else None
        past_kind = 'location' if re.search(r'在哪|去了哪|在哪里|去过哪',message) else 'activity' if re.search(r'做了什么|做什么|干什么|在忙什么',message) else None
        if past_kind and days_ago is not None:
            day = (current-timedelta(days=days_ago)).date().isoformat()
            events = db.execute("""SELECT e.at,e.value FROM events e WHERE e.user=? AND e.kind=?
                AND substr(e.at,1,10)=? AND NOT EXISTS (SELECT 1 FROM events correction
                    WHERE correction.user=e.user AND correction.id>e.id AND correction.previous=e.value
                    AND correction.evidence LIKE '用户明确更正：%')
                ORDER BY e.id DESC LIMIT ?""", (user,past_kind,day,limit)).fetchall()
            return '\n'.join(f'[{stamp}] 用户当时提到：{value}' for stamp,value in events)[:1100]
        rows = db.execute('SELECT f.rowid,f.kind,f.value,f.updated,v.embedding,(SELECT e.evidence FROM events e WHERE e.user=f.user AND e.value=f.value ORDER BY e.id DESC LIMIT 1) FROM facts f LEFT JOIN fact_vectors v ON v.user=f.user AND v.slot=f.kind AND v.value=f.value WHERE f.user=? ORDER BY f.updated DESC,f.rowid DESC', (user,)).fetchall()
        if not rows:
            return ''
        hits = set()
        phrases = sorted(t for t in tokens if len(t)>=3)[:64]
        if phrases and db.execute("SELECT 1 FROM sqlite_master WHERE name='fact_search'").fetchone():
            match = ' OR '.join('"'+t+'"' for t in phrases)
            hits.update(r[0] for r in db.execute('SELECT f.rowid FROM fact_search JOIN facts f ON f.rowid=fact_search.rowid WHERE fact_search MATCH ? AND f.user=? ORDER BY rank LIMIT 40', (match,user)))
    vectors = {}
    embed = encoder()
    if embed:
        import numpy as np
        try:
            q = embed(['为这个句子生成表示以用于检索相关文章：'+query])[0]
            fresh = [r for r in rows if r[4] is None]
            updates = []
            for start in range(0,len(fresh),16):
                batch = fresh[start:start+16]
                for row,vector in zip(batch,embed([embedding_text(r[2], r[5]) for r in batch])):
                    vectors[row[0]] = float(vector @ q)
                    updates.append((vector.tobytes(),user,row[1],row[2]))
            if updates:
                with life.db() as db:
                    db.executemany('INSERT OR REPLACE INTO fact_vectors(embedding,user,slot,value) SELECT ?,user,kind,value FROM facts WHERE user=? AND kind=? AND value=?', updates)
            for row in rows:
                if row[4] is not None:
                    vectors[row[0]] = float(np.frombuffer(row[4],dtype=np.float32) @ q)
        except (ValueError, RuntimeError, OSError) as exc:
            logging.warning('本轮使用词项召回 (%s)', type(exc).__name__)
    nearest = sorted(vectors.values(), reverse=True)
    threshold = .50 if len(nearest)>1 and nearest[0]-nearest[1]>=.08 else .55
    ranked = []
    for ident, slot, value, stamp, _, evidence in rows:
        kind = slot.split(':')[0]
        try:
            at = datetime.fromisoformat(stamp)
            days = max(0,(current-at).total_seconds()/86400)
        except (ValueError,TypeError):
            continue
        if days_ago is not None and at.date() != (current-timedelta(days=days_ago)).date():
            continue
        if kind=='activity' and days>.25 and not re.search('昨天|上次|之前|那天', message):
            continue
        overlap = len(tokens & terms(value))
        category = kind=='preference' and bool(re.search('口味|偏好|我喜欢什么',message))
        semantic = vectors.get(ident, 0)
        if not (overlap or ident in hits or category or semantic >= threshold):
            continue
        relevance = max(min(.98,.55+.10*overlap) if overlap else 0, .75 if category else 0, semantic)
        lifetime = {'plan':8,'activity':4.5,'location':4.5}.get(kind,14)
        score = .75*relevance + .13*math.exp(-days/lifetime) + .08*(ident in hits) + .04*(kind in ('preference','basic_fact'))
        label = {'plan':'当时的计划，未确认完成或仍有效','location':'最后提到的地点，当前未确认'}.get(kind,'用户当时明确提到')
        ranked.append((score, stamp, f'[{stamp}] {label}：{value}'))
    return '\n'.join(item[2] for item in sorted(ranked,reverse=True)[:limit])[:1100]
