"""Bounded, local-only views of structured runtime events."""
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

LOCAL = ZoneInfo('Asia/Shanghai')
LIMIT = 120

LABELS = {
    'received': '收到请求', 'planning': '规划画面', 'queued': '排队',
    'submitted': '已提交 ComfyUI', 'generating': '生成中', 'downloading': '下载图片',
    'generated': '生成完成', 'validating': '检查画面', 'quality_checked': '质检完成',
    'withheld_quality': '质检拦截', 'ready_to_send': '等待发送', 'completed': '任务完成',
    'failed': '失败', 'cancelled': '已取消', 'outcome_unknown': '结果待确认',
    'collected': '收齐消息', 'context_ready': '上下文就绪', 'model_complete': '回复生成完成',
    'typing': '输入中', 'sent': '回复已发送', 'image_sent': '图片已发送',
    'superseded': '回复已替换', 'connection_change': '连接状态变化',
    'login_recovery': '登录恢复', 'supervisor_start': '管理器启动',
}


def read_tail(path, budget=256 * 1024):
    try:
        with path.open('rb') as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - budget))
            if size > budget:
                handle.readline()
            lines = handle.read(budget).splitlines()[-LIMIT:]
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
        except (ValueError, UnicodeError):
            continue
    return rows


def recent_jobs(data):
    dated = []
    for path in (data / 'image_jobs').glob('*/job.json'):
        try:
            dated.append((path.stat().st_mtime, path))
        except OSError:
            continue
    for _, path in sorted(dated, reverse=True)[:24]:
        try:
            job = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(job, dict):
                continue
        except (OSError, ValueError):
            continue
        events = job.get('events') or [dict(at=job.get('created_at'), status=job.get('status'))]
        for event in events[-18:]:
            if isinstance(event, dict):
                yield {**event, 'task': path.parent.name[:8]}


def timestamp(value):
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, LOCAL)
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(LOCAL) if parsed.tzinfo else parsed.replace(tzinfo=LOCAL)
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def token(value):
    return value if isinstance(value, str) and len(value) <= 80 and all(c.isascii() and (c.isalnum() or c in '_-') for c in value) else ''


def entries(root, stream):
    data = Path(root) / 'data'
    sources = {
        'drawing': lambda: recent_jobs(data),
        'reply': lambda: read_tail(data / 'delivery_metrics.jsonl'),
        'login': lambda: read_tail(data / 'login_lifecycle.jsonl'),
    }
    rows = []
    for category, read in sources.items():
        if stream not in ('all', category):
            continue
        for item in read():
            at = timestamp(item.get('at'))
            event = token(item.get('status') if category == 'drawing' else item.get('event'))
            if not at or not event:
                continue
            duration = item.get('elapsed_ms', item.get('total_elapsed_ms'))
            if duration is None and isinstance(item.get('elapsed_seconds'), (int, float)):
                duration = item['elapsed_seconds'] * 1000
            rows.append(dict(at=at.isoformat(timespec='milliseconds'), category=category,
                label=LABELS.get(event, event), reason=token(item.get('reason') or item.get('state')),
                phase=token(item.get('login_phase')), task=token(item.get('task')),
                elapsed_ms=round(duration) if isinstance(duration, (int, float)) and 0 <= duration < 10**9 else None))
    return sorted(rows, key=lambda row: row['at'], reverse=True)[:LIMIT]
