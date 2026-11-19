"""Evidence-grounded facts and private, cumulative character mood state."""
from datetime import datetime
from contextlib import contextmanager
import json
import math
import hashlib
from pathlib import Path
import re
import sqlite3
import memory_recall

DIMENSIONS = ('affinity','trust','joy','anger','sadness','stress','curiosity','calm')
BASE = dict(affinity=25,trust=30,joy=0,anger=0,sadness=0,stress=0,curiosity=15,calm=40)
KINDS = ('location','activity','preference','plan','basic_fact')


def redact(text):
    lines = [line for line in str(text).splitlines() if not re.search(r'密码|密钥|身份证|银行卡|学号|家庭住址|联系电话',line)]
    text = '\n'.join(lines)
    text = re.sub(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', '[邮箱]',text)
    text = re.sub(r'(?<!\d)(?:1[3-9]\d{9}|\d{17}[\dXx]|\d{10})(?!\d)', '[个人号码]',text)
    return text


class Life:
    def __init__(self, root):
        self.root=Path(root)
        self.root.mkdir(parents=True,exist_ok=True)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS facts (user TEXT, kind TEXT, value TEXT, updated TEXT, PRIMARY KEY(user,kind))')
            db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY,user TEXT,at TEXT,kind TEXT,value TEXT,previous TEXT,evidence TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS committed_affect (event_id TEXT PRIMARY KEY, user TEXT, at TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS feelings (user TEXT PRIMARY KEY,data TEXT,updated TEXT)')
            db.execute('CREATE INDEX IF NOT EXISTS event_lookup ON events(user,value,id)')
            memory_recall.initialize(db)
            if 'input_id' not in {r[1] for r in db.execute('PRAGMA table_info(events)')}:
                db.execute('ALTER TABLE events ADD COLUMN input_id TEXT')
        (self.root/'conversation_journal.jsonl').touch(exist_ok=True)

    @contextmanager
    def db(self):
        connection=sqlite3.connect(self.root/'life.db',timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def record(self,user,updates,message,current,diagnostics=None,input_id=None):
        stamp=current.isoformat(timespec='hours')
        accepted=[]
        diagnostics=diagnostics if diagnostics is not None else {}
        updates = updates if isinstance(updates, list) else []
        diagnostics.update(proposed=len(updates),accepted=0,rejected=0,reasons={})
        def reject(reason):
            diagnostics['rejected']+=1
            diagnostics['reasons'][reason]=diagnostics['reasons'].get(reason,0)+1
        for item in updates[:4]:
            if re.search(r'(?:别|不要|不用)(?:把.{0,12})?(?:记住|记录|记下|记)',message):
                reject('user_declined_memory')
                continue
            if not isinstance(item,dict):reject('invalid_item');continue
            kind=item.get('kind');value=str(item.get('value','')).strip()[:160];evidence=str(item.get('evidence','')).strip()
            if re.search(r'假如|假设|开玩笑|模仿|反话|逗你',message):
                reject('hypothetical_or_joke')
                continue
            if kind!='plan' and re.search(r'如果|要是|打算|计划|准备去|想去',evidence):
                reject('plan_as_fact')
                continue
            if kind not in KINDS or not evidence or evidence not in message or not value:
                reject('invalid_kind_value_or_evidence')
                continue
            if kind=='location' and (not re.search(r'我(?:现在)?(?:在|到|回|住|搬)|到了|搬到',evidence) or re.search(r'不在|没到|想去|打算|如果|假如|角色|你在',evidence)):
                reject('unconfirmed_location')
                continue
            if value not in evidence:
                reject('MEMORY_EVIDENCE_FAILED')
                continue
            # A quotation or a third person's activity is not a user-owned fact.
            # A comma may omit the same subject (我不喜欢咖啡，喜欢热茶),
            # but never inherit ownership across a new explicit third person.
            clauses=re.split(r'[，。！？；\n]',evidence)
            owner=False;owned=False
            for clause in clauses:
                if re.match(r'^\s*(?:我(?!(?:听说|听见|听到|发现|觉得|认为|知道|看到)|的?(?:朋友|同学|老师|室友|父母|妈妈|爸爸|哥哥|姐姐|弟弟|妹妹))|本人)',clause):
                    owner=True
                elif re.match(r'^\s*(?:他|她|他们|她们|你|老师|同学|朋友|室友)',clause):
                    owner=False
                elif not re.match(r'^\s*(?:不?喜欢|更喜欢|讨厌|爱|不爱|也|还|并且|不想|想|希望)',clause):
                    owner=False
                if value in clause and owner:owned=True
            # Multi-clause verbatim preference extraction has one self subject.
            if value==evidence and clauses and re.match(r'^我(?:更?喜欢|不喜欢|讨厌)',clauses[0]):
                owned=all(re.match(r'^(?:我)?(?:更?喜欢|不喜欢|讨厌|也|还)',c.strip()) for c in clauses if c.strip())
            if not owned:
                reject('MEMORY_SUBJECT_UNVERIFIED')
                continue
            quoted=any(evidence in quote for quote in re.findall(r'[“「](.*?)[”」]',message,re.S))
            if quoted:
                reject('MEMORY_QUOTED_SPEAKER')
                continue
            value=redact(value)
            if not value:
                reject('sensitive_value')
                continue
            slot=kind if kind in ('location','activity') else kind+':'+hashlib.sha256(value.encode()).hexdigest()[:12]
            current_slot = re.match(r'^我(?:的|现在用的|目前用的)?(耳机|手机|电脑)(?:现在|目前)?(?:是|换成了?|换到了?)', evidence)
            if kind=='basic_fact' and current_slot and not re.search(r'想|如果|打算|以前|之前|曾经',evidence):
                slot = 'basic_fact:current:'+current_slot[1]
            with self.db() as db:
                row=db.execute('SELECT value FROM facts WHERE user=? AND kind=?',(user,slot)).fetchone()
                previous=row[0] if row else None
                if previous==value:
                    reject('unchanged')
                    continue
                db.execute('INSERT INTO facts VALUES (?,?,?,?) ON CONFLICT(user,kind) DO UPDATE SET value=excluded.value,updated=excluded.updated',(user,slot,value,stamp))
                db.execute('INSERT INTO events(user,at,kind,value,previous,evidence,input_id) VALUES (?,?,?,?,?,?,?)',
                           (user,stamp,kind,value,previous,redact(evidence)[:180],input_id))
            event=dict(at=stamp,user=user,kind=kind,value=value,previous=previous,evidence=redact(evidence)[:180])
            accepted.append(event)
            diagnostics['accepted']+=1
            with (self.root/'conversation_journal.jsonl').open('a',encoding='utf-8') as archive:
                archive.write(json.dumps(event,ensure_ascii=False)+'\n')
        return accepted

    def capture_before_trim(self, user, text, current, input_id):
        # Keep only unambiguous first-person statements before bounded history is pruned.
        if re.search(r'别记|不要记|不用记|假设|开玩笑|如果|据说|他说|她说|听说', text):
            return
        if not re.fullmatch(r'我(?:一直|更|最)?(?:不喜欢|喜欢|讨厌)[^“”「」？！?，,。；\n]{1,100}[。！]?', text):
            return
        evidence = text.rstrip('。！')
        self.record(user, [{'kind':'preference','value':evidence,'evidence':evidence}],
                    text, current, input_id=input_id)

    def facts(self,user):
        with self.db() as db:
            rows=db.execute('SELECT kind,value,updated FROM facts WHERE user=? ORDER BY updated DESC',(user,)).fetchall()
        return [dict(kind=k.split(':')[0],value=v,updated=t) for k,v,t in rows]

    def journal(self,limit=30):
        with self.db() as db:
            rows=db.execute('SELECT at,kind,value,previous,evidence FROM events ORDER BY id DESC LIMIT ?',(limit,)).fetchall()
        return [dict(zip(('at','kind','value','previous','evidence'),r)) for r in rows]

    def profile(self,user):
        return '；'.join(f"{x['kind']}={x['value']}" for x in self.facts(user))[:450]

    def recall(self, user, message, current, limit=6, history=()):
        return memory_recall.search(self, user, message, current, limit, history)

    def forget(self, user, value):
        """Explicit exact-value removal; unrelated memories and users are untouched."""
        with self.db() as db:
            count = db.execute('DELETE FROM facts WHERE user=? AND value=?', (user, value)).rowcount
            db.execute('DELETE FROM events WHERE user=? AND value=?', (user, value))
        archive = self.root/'conversation_journal.jsonl'
        lines = archive.read_text(encoding='utf-8').splitlines()
        kept = []
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                kept.append(line)
                continue
            if event.get('user') == user and event.get('value') == value:
                continue
            # A newer event may mention the forgotten value as its old value.
            if event.get('user') == user and event.get('previous') == value:
                event['previous'] = None
            kept.append(json.dumps(event, ensure_ascii=False))
        with self.db() as db:
            db.execute('UPDATE events SET previous=NULL WHERE user=? AND previous=?', (user, value))
        temporary = archive.with_suffix('.tmp')
        temporary.write_text('\n'.join(kept)+ ('\n' if kept else ''), encoding='utf-8')
        temporary.replace(archive)
        return count

    def correct(self,user,old,new,current):
        """Explicit owner-of-the-fact correction: keep dated history, replace active fact."""
        new=redact(new.strip())[:160]
        if not new or new==old:
            return False
        stamp=current.isoformat(timespec='hours')
        with self.db() as db:
            row=db.execute('SELECT kind FROM facts WHERE user=? AND value=?',(user,old)).fetchone()
            if not row:
                return False
            kind=row[0].split(':')[0]
            slot=kind if kind in ('location','activity') else kind+':'+hashlib.sha256(new.encode()).hexdigest()[:12]
            if row[0].startswith('basic_fact:current:'):
                slot = row[0]
            db.execute('DELETE FROM facts WHERE user=? AND kind=?',(user,row[0]))
            db.execute('INSERT INTO facts VALUES (?,?,?,?) ON CONFLICT(user,kind) DO UPDATE SET value=excluded.value,updated=excluded.updated',(user,slot,new,stamp))
            evidence='用户明确更正：'+new
            db.execute('INSERT INTO events(user,at,kind,value,previous,evidence) VALUES (?,?,?,?,?,?)',
                       (user,stamp,kind,new,old,evidence))
        with (self.root/'conversation_journal.jsonl').open('a',encoding='utf-8') as archive:
            archive.write(json.dumps(dict(user=user,at=stamp,kind=kind,value=new,previous=old,evidence=evidence),ensure_ascii=False)+'\n')
        return True

    def mood(self,user,deltas,current,event_id=None):
        with self.db() as db:
            row=db.execute('SELECT data,updated FROM feelings WHERE user=?',(user,)).fetchone()
        values=BASE.copy();mode='neutral'
        if row:
            old=json.loads(row[0]);mode=old.pop('_mode','neutral')
            hours=max(0,(current-datetime.fromisoformat(row[1])).total_seconds()/3600)
            factor=0.5**(hours/12)
            for key in DIMENSIONS:
                values[key]=BASE[key]+(float(old.get(key,BASE[key]))-BASE[key])*factor
        for key in DIMENSIONS:
            value=deltas.get(key,0)
            if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value):
                values[key]=max(0,min(100,values[key]+max(-2,min(2,value))))
        axes={'angry':'anger','sad':'sadness','joyful':'joy','tense':'stress'}
        strongest=max(axes,key=lambda m:values[axes[m]])
        if values[axes[strongest]]>=8:
            mode=strongest
        elif mode not in axes or values[axes[mode]]<4:
            mode='neutral'
        stored={**values,'_mode':mode}
        if deltas:
            with self.db() as db:
                if event_id:
                    inserted=db.execute('INSERT OR IGNORE INTO committed_affect VALUES (?,?,?)',(str(user)+':'+event_id,user,current.isoformat())).rowcount
                    if not inserted:
                        return self.mood(user,{},current)
                db.execute('INSERT OR REPLACE INTO feelings VALUES (?,?,?)',(user,json.dumps(stored),current.isoformat()))
        styles={'neutral':'自然平稳，跟随用户话题。','angry':'语气稍直接但仍尊重用户，不挖苦、威胁、冷暴力或拒绝合理帮助。',
                'sad':'语气稍低缓，不要求用户负责或安慰你。','joyful':'语气更轻快，但不夸张刷屏。','tense':'句子稍简短，仍耐心完成交流。'}
        # Short-term affect must not override the persistent relationship stage.
        style = styles[mode]
        if values['curiosity'] >= BASE['curiosity']+3 and values['trust'] >= BASE['trust']+2:
            style += '可以接续对方提过的兴趣，但只问与当前内容相关的事。'
        if values['stress'] >= BASE['stress']+3 and values['anger'] >= BASE['anger']+2:
            style += '想收住话头时可以简短表态，不用贬低对方。'
        return style
