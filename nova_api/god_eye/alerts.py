from __future__ import annotations
from hashlib import sha256
from datetime import datetime,timezone

def build_alert(kind:str,severity:str,message:str,refs:list[str],created_at:datetime|None=None)->dict:
    created_at=created_at or datetime.now(timezone.utc); key=sha256(f"{kind}|{'|'.join(sorted(refs))}".encode()).hexdigest()
    return {"alert_id":key,"kind":kind,"severity":severity,"message":message,"refs":refs,"created_at":created_at.isoformat(),"acknowledged_at":None,"trade_signal":False}

def confirmation_graph(events:list[dict],posts:list[dict])->dict:
    edges=[]
    for event in events:
        for source in event.get("source_ids",[]):edges.append({"from":f"source:{source}","to":f"event:{event['event_id']}","type":"reported"})
        for entity in event.get("entities",[]):edges.append({"from":f"entity:{entity}","to":f"event:{event['event_id']}","type":"mentioned"})
    for post in posts:
        author=post.get("author",{}).get("author_id","unknown"); edges.append({"from":f"author:{author}","to":f"post:{post['post_id']}","type":"authored"})
    independent=len({edge["from"] for edge in edges if edge["type"] in {"reported","authored"}})
    return {"edges":edges,"independent_source_count":independent}
