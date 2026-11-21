import hashlib, json
from datetime import datetime, timezone
from app.config import RULE_VERSION

def digest(raw, context, evidence=None):
    """批次身份只由不可变输入与规则版本决定。

    口径切换是同一批输入上的决定，不改变批号；它作为批次内的版本登记，
    这样同一指令重试不会凭空产生新批次。
    """
    evidence = evidence or {}
    payload = raw + json.dumps(context, ensure_ascii=False, sort_keys=True).encode() + RULE_VERSION.encode()
    for kind in sorted(evidence):
        payload += kind.encode() + b'\x00' + (evidence[kind] or b'') + b'\x00'
    return hashlib.sha256(payload).hexdigest()

def make_manifest(source_digest, source, elapsed, evidence_source=None):
    return {'run_id':source_digest[:16], 'sha256':source_digest, 'rule_version':RULE_VERSION,
            'source':source, 'evidence_source':evidence_source,
            'generated_at':datetime.now(timezone.utc).isoformat(),
            'elapsed_ms':round(elapsed*1000,3), 'timezone':'Asia/Shanghai'}
