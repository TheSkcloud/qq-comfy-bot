"""Shared complete model fixture for transport/queue tests; not used by production."""
def creative(action='reading a book'):
    return dict(concept='书页停在翻动的一瞬',narrative_moment='正要翻到下一页',subject='the saved adult character',
        action=action,expression='calm expression',gesture='natural relaxed posture',props='an open book',
        environment='the saved location and outfit',atmosphere='quiet natural indoor light',focal_point='the current activity',
        emotional_intent='分享安静的一刻',context_connection='接续当前已建立的角色活动',creative_reason='这个停顿能表现当前活动的细节')
