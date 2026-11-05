from datetime import date, timedelta
import json
from pathlib import Path

PATH = Path(__file__).with_name('timetable.json')


def load():
    return json.loads(PATH.read_text(encoding='utf-8'))


def in_weeks(week, expression):
    for item in expression.split(','):
        if '-' in item:
            low, high = map(int, item.split('-'))
            if low <= week <= high:
                return True
        elif week == int(item):
            return True
    return False


def week_number(day, config=None):
    config = config or load()
    anchor = date.fromisoformat(config['anchor_date'])
    anchor_monday = anchor-timedelta(days=anchor.weekday())
    monday = day-timedelta(days=day.weekday())
    return config['anchor_week'] + (monday-anchor_monday).days//7


def today(current):
    config = load()
    week = week_number(current.date(), config)
    courses = [dict(c) for c in config['courses'] if c['day']==current.weekday() and in_weeks(week,c['weeks'])]
    for course in courses:
        if course['name']=='大学物理实验(1)' and week >= 9:
            course['room'] = f'大物实验室{week-7}'
    part = 'morning' if 8<=current.hour<13 else 'afternoon' if 14<=current.hour<19 else 'evening' if 19<=current.hour<23 else None
    possible = [c['name'] for c in courses if c['part']==part]
    return dict(week=week, courses=courses, possible=possible, uncertain=True, note=config['note'])


def context(current):
    data = today(current)
    names = '；'.join(f"{c['periods']}节{c['name']}" for c in data['courses']) or '无固定课程'
    possible = '、'.join(data['possible']) or '无可据课表推断的活动'
    return f"课表第{data['week']}周，今日：{names}。当前时段可能：{possible}。无准确作息，仅供猜测；用户陈述优先，不能断言已在上课或教室。"
