"""No network I/O: bounded, opt-in, private fictional-life initiative."""
from datetime import datetime
from zoneinfo import ZoneInfo
import image_director


def eligible(session, current, settings, connected=True):
    if not settings.get('proactive_enabled',False) or not settings.get('spontaneous_images',True) or not connected:
        return False
    cfg=image_director.config(settings.get('drawing'))
    local=current.astimezone(ZoneInfo(cfg['timezone']))
    start,end=cfg['quiet_start'],cfg['quiet_end']
    if (start<=local.hour<end if start<end else local.hour>=start or local.hour<end if start!=end else False):
        return False
    try:
        last=datetime.fromisoformat(session['last_user_at'])
        # No first-contact spam and no catch-up after long absences/reboots.
        if not cfg['life_inactive_min_minutes']*60 <= (current-last).total_seconds() <= cfg['life_contact_window_days']*86400:
            return False
        if session.get('busy_until') and datetime.fromisoformat(session['busy_until'])>current:
            return False
        if session.get('initiative_at') and (current-datetime.fromisoformat(session['initiative_at'])).total_seconds()<cfg['life_min_interval_minutes']*60*(1 + .5*min(4,unanswered(session))):
            return False
    except (KeyError,ValueError,TypeError):
        return False
    if sum(a.get('day')==local.date().isoformat() for a in session.get('initiative_attempts',[]))>=cfg['life_candidates_per_day']:
        return False
    if sum(a.get('day')==local.date().isoformat() and a.get('kind')!='explicit' for a in session.get('drawing_sends',[]))>=cfg['daily_budget']:
        return False
    return True


def unanswered(session):
    last_user = session.get('last_user_at', '')
    return sum(bool(item.get('initiative')) and item.get('at','') > last_user
               for item in session.get('drawing_sends', []))


def worthwhile(session, activity):
    count = unanswered(session)
    if not count:
        return True
    recent = [s for s in session.get('drawing_sends', []) if s.get('initiative')]
    if recent and recent[-1].get('activity') == activity:
        return False
    if count < 2:
        return True
    import re
    from memory_recall import terms
    previous = next((m['content'] for m in reversed(session.get('history', [])) if m.get('role')=='user'), '')
    return bool(terms(activity) & terms(previous) or re.search(r'比赛|演出|庆祝|纪念|完成|生日',activity))
