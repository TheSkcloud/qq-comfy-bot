"""Single persistence gateway around versioned session storage and ACK effects."""
import delivery_state

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
        delivery_state.acknowledge(session,data,spoken,source,ack,current,complete)
        if receipt is not None and data.get('carry_resolution') in ('included','irrelevant'):
            session.pop('pending_essential',None)
        if receipt is not None and complete and affect_user is not None and data.get('affect'):
            session.setdefault('pending_affect',{})[input_id]={'user':affect_user,'delta':data['affect'],'at':current.isoformat()}
        self.save(key,session)
        return session,receipt is not None

    def drain_affect(self,key,life):
        from datetime import datetime
        session=self.get(key)
        for identifier,item in list(session.get('pending_affect',{}).items()):
            self.effect(life.mood,item['user'],item['delta'],datetime.fromisoformat(item['at']),event_id=identifier)
            session['pending_affect'].pop(identifier,None)
            self.save(key,session)
        return session

    def recover(self,life):
        import json
        for key,value in self.db.execute('SELECT key,value FROM sessions').fetchall():
            if json.loads(value).get('pending_affect'):self.drain_affect(key,life)
