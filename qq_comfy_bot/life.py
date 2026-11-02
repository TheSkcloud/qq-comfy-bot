"""Evidence-grounded facts and private, cumulative character mood state."""
from datetime import datetime
from contextlib import contextmanager
import json
import math
import hashlib
from pathlib import Path
import re
import sqlite3

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
            db.execute('CREATE TABLE IF NOT EXISTS feelings (user TEXT PRIMARY KEY,data TEXT,updated TEXT)')
        (self.root/'conversation_journal.jsonl').touch(exist_ok=True)

    @contextmanager
    def db(self):
        connection=sqlite3.connect(self.root/'life.db',timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def record(self,user,updates,message,current):
        stamp=current.isoformat(timespec='hours')
        accepted=[]
        for item in updates[:4]:
            kind=item.get('kind');value=str(item.get('value','')).strip()[:160];evidence=str(item.get('evidence','')).strip()
            if re.search(r'假如|假设|开玩笑|模仿|反话|逗你',message):
                continue
            if kind!='plan' and re.search(r'如果|要是|打算|计划|准备去|想去',evidence):
                continue
            if kind not in KINDS or not evidence or evidence not in message or not value:
                continue
            if kind=='location' and (not re.search(r'我(?:现在)?(?:在|到|回|住|搬)|到了|搬到',evidence) or re.search(r'不在|没到|想去|打算|如果|假如|角色|你在',evidence)):
                continue
            if kind=='location' and value not in evidence:
                continue
            value=redact(value)
            if not value:
                continue
            slot=kind if kind in ('location','activity') else kind+':'+hashlib.sha256(value.encode()).hexdigest()[:12]
            with self.db() as db:
                row=db.execute('SELECT value FROM facts WHERE user=? AND kind=?',(user,slot)).fetchone()
                previous=row[0] if row else None
                if previous==value:
                    continue
                db.execute('INSERT OR REPLACE INTO facts VALUES (?,?,?,?)',(user,slot,value,stamp))
                db.execute('INSERT INTO events(user,at,kind,value,previous,evidence) VALUES (?,?,?,?,?,?)',
                           (user,stamp,kind,value,previous,redact(evidence)[:180]))
            event=dict(at=stamp,user=user,kind=kind,value=value,previous=previous,evidence=redact(evidence)[:180])
            accepted.append(event)
            with (self.root/'conversation_journal.jsonl').open('a',encoding='utf-8') as archive:
                archive.write(json.dumps(event,ensure_ascii=False)+'\n')
        return accepted

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

    def recall(self, user, message, current, limit=6):
        """Retrieve relevant facts with their dates, never present stale activity as current."""
        terms = set(re.findall(r'[a-zA-Z]{3,}', message.lower()))
        terms.update(message[i:i+2] for i in range(len(message)-1)
                     if all('\u4e00' <= char <= '\u9fff' for char in message[i:i+2]))
        terms -= {'今天', '现在', '这个', '那个', '什么', '怎么', '一下', '可以', '我的'}
        ranked = []
        for fact in self.facts(user):
            try:
                at = datetime.fromisoformat(fact['updated'])
                hours = max(0, (current-at).total_seconds()/3600)
            except (ValueError, TypeError):
                hours = 99999
            kind, value = fact['kind'], fact['value']
            overlap = sum(term in value.lower() for term in terms)
            if kind == 'activity' and hours > 6:
                continue
            if kind == 'plan' and hours > 7*24 and not overlap:
                continue
            if not overlap and kind in ('location','activity','plan'):
                continue
            score = overlap*10 + (3 if kind in ('preference', 'basic_fact') else 1) + 1/(1+hours)
            label = '当时的计划，未确认完成或仍有效' if kind == 'plan' else (
                '最后提到的地点，当前未确认' if kind == 'location' else '用户当时明确提到')
            ranked.append((score, f"[{fact['updated']}] {label}：{value}"))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return '\n'.join(value for score, value in ranked[:limit])[:1100]

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
            db.execute('DELETE FROM facts WHERE user=? AND kind=?',(user,row[0]))
            db.execute('INSERT OR REPLACE INTO facts VALUES (?,?,?,?)',(user,slot,new,stamp))
            evidence='用户明确更正：'+new
            db.execute('INSERT INTO events(user,at,kind,value,previous,evidence) VALUES (?,?,?,?,?,?)',
                       (user,stamp,kind,new,old,evidence))
        with (self.root/'conversation_journal.jsonl').open('a',encoding='utf-8') as archive:
            archive.write(json.dumps(dict(user=user,at=stamp,kind=kind,value=new,previous=old,evidence=evidence),ensure_ascii=False)+'\n')
        return True

    def mood(self,user,deltas,current):
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
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO feelings VALUES (?,?,?)',(user,json.dumps(stored),current.isoformat()))
        styles={'neutral':'自然平稳，跟随用户话题。','angry':'语气稍直接但仍尊重用户，不挖苦、威胁、冷暴力或拒绝合理帮助。',
                'sad':'语气稍低缓，不要求用户负责或安慰你。','joyful':'语气更轻快，但不夸张刷屏。','tense':'句子稍简短，仍耐心完成交流。'}
        # Short-term affect must not override the persistent relationship stage.
        return styles[mode]
