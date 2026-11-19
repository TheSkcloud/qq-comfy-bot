"""Single persistence gateway around versioned session storage and ACK effects."""
import delivery_state
import turn_taking
import logging

class StateCommitter:
    def __init__(self,store,coordinator):
        self.store=store;self.coordinator=coordinator
    @property
    def db(self):return self.store.db
    def get(self,key):return self.store.get(key)
    def save(self,key,session):
        with self.coordinator.state():
            self.store.save(key,session)
            self.coordinator.record('state','committed')
    def effect(self,function,*args,**kwargs):
        with self.coordinator.state():
            result=function(*args,**kwargs)
            self.coordinator.record('state','effect_committed')
            return result
    def acknowledge(self,key,input_id,data,spoken,source,ack,current,complete=False,affect_user=None):
        session=self.get(key)
        pending=session.get('pending_turn',{})
        if pending.get('id')!=input_id:return session,False
        receipt=(ack or {}).get('message_id')
        if receipt is not None and str(receipt) in pending.get('committed_receipts',[]):return session,False
        # A later scene change cannot be overwritten by a delayed old reply.
        if session.get('scene_version',0)!=pending.get('base_scene_version',0) and not pending.get('applied_fields'):
            pending['changes']=[]
        if receipt is not None and affect_user is not None and not pending.get('committed_receipts'):
            from companion import confirmed_style
            confirmed_style(session, input_id, source, current)
        delivery_state.acknowledge(session,data,spoken,source,ack,current,complete)
        if receipt is not None and data.get('carry_resolution') in ('included','irrelevant'):
            session.pop('pending_essential',None)
        if receipt is not None and complete and affect_user is not None and data.get('affect'):
            session.setdefault('pending_affect',{})[input_id]={'user':affect_user,'delta':data['affect'],'at':current.isoformat()}
        plan = session.get('delivery_plan', {})
        if receipt is not None and plan.get('version') == pending.get('reply_version'):
            for segment in plan.get('segments', []):
                if segment.get('status') in ('sending', 'unknown'):
                    segment.update(status='sent', qq_result=ack, at=current.isoformat())
                    record = {'episode_id': plan.get('id'), 'version': plan['version'], 'segment': segment['index'],
                              'state': 'sent', 'qq_result': ack, 'at': current.isoformat()}
                    session['delivery_receipts'] = (session.get('delivery_receipts', []) + [record])[-50:]
                    break
        self.save(key,session)
        return session,receipt is not None

    def finish_delivered_prefix(self, key, input_id, current):
        session = self.get(key)
        pending = session.get('pending_turn', {})
        episode = session.get('delivery_plan', {})
        if pending.get('id') != input_id or pending.get('status') != 'partially_acknowledged':
            return session
        if not pending.get('committed_receipts') or any(s['status'] not in ('sent','cancelled') for s in episode['segments']):
            return session
        pending['status'] = 'acknowledged'
        affect = episode.get('ack_fields', {}).get('affect')
        if affect and episode.get('affect_user'):
            session.setdefault('pending_affect', {})[input_id] = {'user':episode['affect_user'], 'delta':affect, 'at':current.isoformat()}
        self.save(key, session)
        return session

    def drain_affect(self,key,life):
        from datetime import datetime
        session=self.get(key)
        for identifier,item in list(session.get('pending_affect',{}).items()):
            try:
                self.effect(life.mood,item['user'],item['delta'],datetime.fromisoformat(item['at']),event_id=identifier)
            except Exception:
                logging.exception('情绪更新待重试')
                continue
            session['pending_affect'].pop(identifier,None)
            self.save(key,session)
        return session

    def late_receipt(self, echo, result, current):
        # Only SnowLuma's original correlation ID proves which send was acknowledged.
        ack = result.get('data') or {}
        rejected = result.get('retcode') not in (None, 0)
        if not rejected and not (isinstance(ack, dict) and ack.get('message_id')):
            return
        for (key,) in self.db.execute('SELECT key FROM sessions WHERE instr(value, ?) > 0', (echo,)).fetchall():
            session = self.get(key)
            plans = [session.get('delivery_plan', {}), *session.get('unknown_deliveries', [])]
            for plan in plans:
                for segment in plan.get('segments', []):
                    if segment.get('echo') != echo or segment['status'] not in ('sending', 'unknown'):
                        continue
                    pending = session.get('pending_turn', {})
                    current_turn = bool(plan.get('input_id')) and pending.get('id') == plan['input_id']
                    if not rejected and current_turn:
                        complete = all(s is segment or s['status'] == 'sent' for s in plan['segments'])
                        self.acknowledge(key, plan['input_id'], plan['ack_fields'], segment['text'], plan['source'],
                                         ack, current, complete, affect_user=plan.get('affect_user'))
                        return
                    if rejected:
                        if current_turn:
                            pending['status'] = 'failed'
                        segment.update(status='failed', reason='qq_send_rejected')
                    else:
                        segment.update(status='sent', qq_result=ack, at=current.isoformat())
                        session['delivery_receipts'] = (session.get('delivery_receipts', []) + [{
                            'episode_id': plan['id'], 'version': plan['version'], 'segment': segment['index'],
                            'state': 'sent', 'qq_result': ack, 'at': current.isoformat()}])[-50:]
                        entry = {'id': segment['id'], 'role': 'assistant', 'content': segment['text'],
                                 'at': segment['dispatched_at']}
                        history = session.get('history', []) + [entry]
                        session['history'] = sorted(history, key=lambda x: x.get('at', ''))[-20:]
                    self.save(key, session)
                    return

    def recover(self,life):
        import preferences
        recover_turns = turn_taking.config(preferences.load().get("turn_taking"))["recovery"]["enabled"]
        for key,value in self.db.execute('SELECT key,value FROM sessions').fetchall():
            session = self.get(key)
            plan = session.get('delivery_plan', {})
            if recover_turns:
                if turn_taking.settle(plan, 'process_restarted', session.get('delivery_receipts', [])):
                    pending = session.get('pending_turn', {})
                    if pending.get('reply_version') == plan.get('version'):
                        pending['status'] = 'outcome_unknown' if any(s['status']=='unknown' for s in plan['segments']) else 'interrupted'
                    self.save(key, session)
            if session.get('pending_affect'):
                self.drain_affect(key,life)
