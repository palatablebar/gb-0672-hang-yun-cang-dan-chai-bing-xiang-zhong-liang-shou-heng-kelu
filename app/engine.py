from importlib import import_module
from copy import deepcopy
import json, time, base64
from app.config import ROOT, MODULES, MAX_BYTES
from app.errors import BusinessError
from app.manifest import digest, make_manifest

EVIDENCE_KINDS = ('weighing', 'tare', 'seal')
DATASETS = {'dirty', 'clean', 'legacy'}


def _read_evidence(dataset):
    """读取与主数据同名的三类证据；缺失文件按“无证据”处理，不伪造。"""
    files = {}
    for kind in EVIDENCE_KINDS:
        path = ROOT / 'sample_data' / f'{kind}_{dataset}.csv' if dataset else None
        files[kind] = path.read_bytes() if path is not None and path.exists() else b''
    return files


def _evidence_from_snapshot(snapshot):
    return {kind: base64.b64decode(snapshot['evidence_b64'].get(kind, ''), validate=True)
            for kind in EVIDENCE_KINDS}


def normalize_instructions(value):
    """切换指令必须是对象；同箱只允许一个口径，排序后参与批内版本摘要。"""
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise BusinessError('BAD_INSTRUCTION', '切换指令须为对象')
    switches = value.get('switches', value)
    if isinstance(switches, dict):
        switches = [{'container': key, **(item if isinstance(item, dict) else {'basis': item})}
                    for key, item in switches.items()]
    if not isinstance(switches, list):
        raise BusinessError('BAD_INSTRUCTION', 'switches 须为数组')
    normalized = {}
    for item in switches:
        if not isinstance(item, dict):
            raise BusinessError('BAD_INSTRUCTION', '单个切换须为对象')
        container = str(item.get('container', '')).strip()
        if not container:
            raise BusinessError('MISSING_ID', '切换缺箱号')
        basis = str(item.get('basis', 'declared')).strip()
        if basis not in {'weighing', 'declared'}:
            raise BusinessError('BAD_BASIS', f'{container} 口径须为 weighing 或 declared')
        entry = {'container': container, 'basis': basis, 'note': str(item.get('note', '')).strip()}
        if container in normalized and normalized[container] != entry:
            raise BusinessError('BAD_INSTRUCTION', f'{container} 同一指令内口径冲突')
        normalized[container] = entry
    return {'switches': [normalized[key] for key in sorted(normalized)]} if normalized else {}


def merge_instructions(current, update):
    """新增/覆盖本次点到的箱，未点到的箱保留原决定。"""
    merged = {item['container']: item for item in normalize_instructions(current).get('switches', [])}
    for item in normalize_instructions(update).get('switches', []):
        merged[item['container']] = item
    return {'switches': [merged[key] for key in sorted(merged)]} if merged else {}


def evaluate(payload=None, until=8, snapshot=None):
    payload = payload or {}
    dataset = payload.get('dataset', 'dirty')
    if dataset not in DATASETS: raise BusinessError('BAD_DATASET', '样例名称无效')
    if not isinstance(payload.get('overrides', {}), dict): raise BusinessError('BAD_CONTEXT', '参数须为对象')
    raw = (base64.b64decode(snapshot['bytes_b64'], validate=True) if snapshot is not None
           else (str(payload['csv_text']).encode() if 'csv_text' in payload
                 else (ROOT / 'sample_data' / f'{dataset}.csv').read_bytes()))
    if len(raw) > MAX_BYTES: raise BusinessError('TOO_LARGE', '文件超过2MB', 413)
    context = deepcopy(snapshot['context']) if snapshot is not None else json.loads((ROOT / 'sample_data/context.json').read_text())
    context.update(deepcopy(payload.get('overrides', {})))

    if snapshot is not None:
        evidence, evidence_source = _evidence_from_snapshot(snapshot), snapshot.get('evidence_source')
    else:
        evidence_source = payload.get('evidence_dataset', dataset if 'csv_text' not in payload else None)
        if evidence_source is not None and evidence_source not in DATASETS:
            raise BusinessError('BAD_DATASET', '证据样例名称无效')
        evidence = _read_evidence(evidence_source)
    if 'handover' in payload:
        instructions = normalize_instructions(payload['handover'])
    elif snapshot is not None:
        instructions = normalize_instructions(snapshot.get('handover'))
    else:
        instructions = {}

    start = time.perf_counter()
    state = {'raw_bytes': raw, 'context': context, 'records': [], 'quarantine': [], 'counts': {}, 'views': {},
             'source': dataset, 'evidence_source': evidence_source,
             'evidence': {**evidence, 'names': {
                 kind: f'{kind}_{evidence_source}.csv' if evidence_source else '（未提供）'
                 for kind in EVIDENCE_KINDS}},
             'handover_instructions': deepcopy(instructions)}
    for index, key in enumerate(MODULES, 1):
        if index > until: break
        import_module(f'app.modules.{key}.rules').apply(state)
    state['input_snapshot'] = {
        'bytes_b64': base64.b64encode(raw).decode(),
        'evidence_b64': {kind: base64.b64encode(evidence.get(kind, b'')).decode() for kind in EVIDENCE_KINDS},
        'evidence_source': evidence_source,
        'handover': deepcopy(instructions),
        'context': deepcopy(context),
    }
    state.pop('evidence', None)
    state.pop('raw_bytes', None)
    state['manifest'] = make_manifest(digest(raw, context, evidence), dataset,
                                      time.perf_counter() - start, evidence_source)
    return state
