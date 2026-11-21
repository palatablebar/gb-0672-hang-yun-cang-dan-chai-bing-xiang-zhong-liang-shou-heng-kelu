"""箱门重量交接：把码头整箱毛重、皮重台账与封签台账按箱拼成可复核的交接单。

三种证据来自不同记录，不能相互覆盖：
- 码头/地磅只给整箱毛重，并附当时读到的一个封签号；
- 皮重台账只给空箱皮重；
- 封签台账是申报连续性所依据的原始封签，任何情况下不得被称重记录覆盖。

箱内货毛重 = 整箱毛重 - 皮重。只有该结果可复核、且与舱单逐箱货物对得上时，才采用
称重口径；否则保留原申报货重不动，把差异挂成阻断，等差异说明后才改口径。

口径切换由 instruction 明确指定，切换前先校验可复核性；校验不过直接报错，
调用方事务回滚，旧版与原始封签原样保留。
"""
import base64
import csv
import io
from decimal import Decimal, InvalidOperation
from app.coerce import number
from app.errors import BusinessError

WEIGH_FIELDS = ['container', 'terminal_gross', 'weight_unit', 'seal', 'source', 'weighed_at', 'revision']
TARE_FIELDS = ['container', 'tare', 'weight_unit', 'source', 'revision']
SEAL_FIELDS = ['container', 'seal', 'source', 'revision']
SOURCE_NAMES = {'weighing': '称重记录', 'tare': '皮重台账', 'seal': '封签台账'}
UNITS = {'kg': Decimal(1), 't': Decimal(1000)}
ZERO = Decimal(0)
BASIS_WEIGHING = 'weighing'
BASIS_DECLARED = 'declared'
BASES = (BASIS_WEIGHING, BASIS_DECLARED)
BASIS_LABEL = {BASIS_WEIGHING: '可复核称重口径', BASIS_DECLARED: '原申报（保全）'}
BASIS_NOTE = {BASIS_WEIGHING: '申报货重已按可复核称重结果更新', BASIS_DECLARED: '保留原申报货重，差异未说明前不改口径'}
DECLARED_NOTE = {
    'NO_WEIGHING': '保留原申报货重，待地磅结果',
    'NO_TARE': '保留原申报货重，缺皮重无法还原箱内货毛重',
    'NO_SEAL_LEDGER': '保留原申报货重，缺原申报封签记录',
    'SEAL_MISMATCH': '保留原申报货重，封签与申报不一致待核',
    'WEIGHT_DELTA': '保留原申报货重，称重差异未说明',
    'UNROOTED': '保留原申报货重，货物无法追溯到主单',
    'NO_MANIFEST_CARGO': '无舱单货，不能交接',
}

def _decode(raw):
    if not raw or not raw.strip():
        return None
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return None


def _value(row, field):
    unit = row.get('weight_unit', 'kg').strip() or 'kg'
    if unit not in UNITS:
        raise BusinessError('BAD_UNIT', f'重量单位不受支持: {unit}')
    return number(row.get(field, '')) * UNITS[unit]


def _revision(row):
    value = number(row.get('revision', ''))
    if value < 1 or value % 1:
        raise BusinessError('INVALID_RANGE', '版本号须为正整数')
    return int(value)


def read_evidence(raw, name, fields):
    """解析单个证据文件，返回 (按箱索引, 隔离行, 证据元信息)。

    空文件不是异常：交接单要显示“缺证据”，而不是让整个批次失败。
    """
    label = SOURCE_NAMES[name]
    meta = {'name': name, 'label': label, 'rows': 0, 'encoding': None}
    decoded = _decode(raw)
    if decoded is None:
        return {}, [], meta, [{'code': 'NO_EVIDENCE', 'source': name, 'detail': f'{label}无可用记录'}]
    text, encoding = decoded
    meta['encoding'] = encoding

    stream = csv.reader(io.StringIO(text, newline=''), strict=True)
    try:
        original = next(stream)
        header = [cell.strip() for cell in original]
        if header != fields:
            return {}, [], meta, [{'code': 'BAD_HEADER', 'source': name,
                                   'detail': f'{label}表头应为 {",".join(fields)}'}]
        parsed, rejected = [], []
        for cells in stream:
            line = stream.line_num
            if not cells or not any(cell.strip() for cell in cells) or cells == original:
                continue
            if len(cells) != len(header):
                rejected.append({'code': 'COLUMN_COUNT', 'source': name, 'line': line,
                                 'detail': f'{label}需要{len(header)}列，实际{len(cells)}列'})
                continue
            row = dict(zip(header, cells))
            row['container'] = row.get('container', '').strip()
            if not row['container']:
                rejected.append({'code': 'MISSING_ID', 'source': name, 'line': line,
                                 'detail': f'{label}箱号为空'})
                continue
            try:
                if name == 'weighing':
                    row['terminal_gross_kg'] = str(_value(row, 'terminal_gross'))
                    row['weighed_seal'] = row.get('seal', '').strip()
                    if not row['weighed_seal']:
                        raise BusinessError('MISSING_VALUE', '称重记录缺封签')
                elif name == 'tare':
                    row['tare_kg'] = str(_value(row, 'tare'))
                else:
                    row['seal'] = row.get('seal', '').strip()
                    if not row['seal']:
                        raise BusinessError('MISSING_VALUE', '封签台账缺封签号')
                row['revision'] = _revision(row)
            except BusinessError as error:
                rejected.append({'code': error.code, 'source': name, 'line': line,
                                 'container': row['container'], 'detail': error.message})
                continue
            row['_line'] = line
            parsed.append(row)

        meta['rows'] = len(parsed)
        grouped = {}
        for row in parsed:
            grouped.setdefault(row['container'], []).append(row)
        indexed = {}
        for container, rows in grouped.items():
            identities = {tuple((k, v) for k, v in row.items() if k != '_line') for row in rows}
            if len(identities) > 1:
                for row in rows:
                    rejected.append({'code': 'DUPLICATE_CONFLICT', 'source': name, 'line': row['_line'],
                                     'container': container,
                                     'detail': f'{label}同一箱存在不同原值'})
                continue
            indexed[container] = rows[0]
        return indexed, rejected, meta, []
    except csv.Error:
        return {}, [], meta, [{'code': 'BROKEN_CSV', 'source': name,
                               'detail': f'{label}引号或末行损坏'}]


def _children(records):
    by_id = {row['id']: row for row in records}
    children = {key: [] for key in by_id}
    for row in records:
        parent = row['parent'].strip()
        if parent and parent in by_id:
            children[parent].append(row['id'])
    for key in children:
        children[key].sort()
    return children


def _root_of(node, parent):
    seen = set()
    current = node
    while True:
        if current in seen:
            return None
        seen.add(current)
        p = parent.get(current, '')
        if not p:
            return current
        if p not in parent:
            return None
        current = p


def _sum(rows, field):
    return sum((Decimal(str(row[field])) for row in rows), ZERO)


def _fmt(value):
    value = Decimal(value)
    if value == 0:
        return '0'
    if value == value.to_integral():
        return format(value.quantize(Decimal(1)), 'f')
    return format(value.normalize(), 'f')


def _tolerance(context):
    try:
        value = Decimal(str(context.get('handover', {}).get('tolerance_kg', '0')))
    except (InvalidOperation, ValueError):
        value = ZERO
    return value if value >= 0 else ZERO


def _evidence_text(weigh, tare, seal, names):
    parts = []
    if weigh:
        parts.append(f"毛重 {weigh.get('source', '?')}@{names['weighing']}#{weigh['_line']} rev{weigh['revision']}")
    if tare:
        parts.append(f"皮重 {tare.get('source', '?')}@{names['tare']}#{tare['_line']} rev{tare['revision']}")
    if seal:
        parts.append(f"封签 {seal.get('source', '?')}@{names['seal']}#{seal['_line']} rev{seal['revision']}")
    return '；'.join(parts)


def _normalize_instructions(instructions):
    """校验 instruction 结构，返回 {箱号: {'basis':..., 'note':...}}。"""
    if instructions in (None, {}):
        return {}
    if not isinstance(instructions, dict):
        raise BusinessError('BAD_INSTRUCTION', '切换指令须为对象')
    raw = instructions.get('switches', instructions)
    if isinstance(raw, dict):
        raw = [{'container': key, **(value if isinstance(value, dict) else {'basis': value})}
               for key, value in raw.items()]
    if not isinstance(raw, list):
        raise BusinessError('BAD_INSTRUCTION', 'switches 须为数组')
    normalized = {}
    for item in raw:
        if not isinstance(item, dict):
            raise BusinessError('BAD_INSTRUCTION', '单个切换须为对象')
        container = str(item.get('container', '')).strip()
        if not container:
            raise BusinessError('MISSING_ID', '切换缺箱号')
        basis = str(item.get('basis', BASIS_DECLARED)).strip()
        if basis not in BASES:
            raise BusinessError('BAD_BASIS', f'{container} 口径须为 weighing 或 declared')
        entry = {'basis': basis, 'note': str(item.get('note', '')).strip()}
        if container in normalized and normalized[container] != entry:
            raise BusinessError('BAD_INSTRUCTION', f'{container} 同一指令内口径冲突')
        normalized[container] = entry
    return normalized


def _evidence(state):
    """优先用内存中的原始证据；落库后的批次只能从不可变快照还原。"""
    if 'evidence' in state:
        return state['evidence'], state.get('evidence_source')
    snapshot = state.get('input_snapshot') or {}
    decoded = {kind: base64.b64decode(snapshot.get('evidence_b64', {}).get(kind, ''), validate=True)
               for kind in ('weighing', 'tare', 'seal')}
    return decoded, snapshot.get('evidence_source')


def build(state, instructions=None):
    requested = _normalize_instructions(instructions)
    evidence, evidence_source = _evidence(state)
    names = evidence.get('names') or {kind: f'{kind}_{evidence_source}.csv' if evidence_source else '（未提供）'
                                      for kind in ('weighing', 'tare', 'seal')}
    ledgers = {}
    rejected = []
    metas = {}
    for key, fields in (('weighing', WEIGH_FIELDS), ('tare', TARE_FIELDS), ('seal', SEAL_FIELDS)):
        indexed, bad, meta, hard = read_evidence(evidence.get(key, b''), key, fields)
        ledgers[key] = indexed
        metas[key] = meta
        rejected.extend(bad)
        rejected.extend(hard)
    weighing, tare_ledger, seal_ledger = ledgers['weighing'], ledgers['tare'], ledgers['seal']

    records = state['records']
    children = _children(records)
    by_id = {row['id']: row for row in records}
    parent = {row['id']: row['parent'].strip() for row in records}
    container_of = {row['id']: row.get('container', '').strip() for row in records}
    roots = sorted(row['id'] for row in records if not row['parent'].strip())

    reachable, stack = set(), list(roots)
    while stack:
        node = stack.pop()
        if node in reachable:
            continue
        reachable.add(node)
        stack.extend(children.get(node, []))

    unrooted = sorted(set(by_id) - reachable)
    no_container = sorted(node for node, container in container_of.items() if not container)
    assigned = {}
    for node in sorted(reachable):
        assigned.setdefault(container_of[node], []).append(node)

    graph_errors = state.get('graph', {}).get('errors', [])
    weight_checks = state.get('weight_checks', [])
    hazard_issues = state.get('hazard_issues', [])
    tolerance = _tolerance(state['context'])

    containers = set(assigned) | set(weighing) | set(tare_ledger) | set(seal_ledger)
    containers.discard('')
    unknown = sorted(set(requested) - containers)
    if unknown:
        raise BusinessError('UNKNOWN_REFERENCE', '切换目标箱不在本批：' + ','.join(unknown))

    rows = []
    for container in sorted(containers):
        nodes = sorted(assigned.get(container, []))
        node_set = set(nodes)
        unrooted_here = [node for node in unrooted if container_of.get(node) == container]
        leaves = [node for node in nodes if not children.get(node) and node in reachable]
        root_ids = sorted({root for root in (_root_of(node, parent) for node in leaves) if root})
        leaf_rows = [by_id[node] for node in leaves]
        pieces = sum(by_id[node]['pieces'] for node in leaves)
        net = _sum(leaf_rows, 'net_kg')
        gross = _sum(leaf_rows, 'gross_kg')
        root_gross = _sum([by_id[root] for root in root_ids], 'gross_kg')

        weigh = weighing.get(container)
        tare = tare_ledger.get(container)
        seal = seal_ledger.get(container)
        terminal = Decimal(weigh['terminal_gross_kg']) if weigh else None
        tare_kg = Decimal(tare['tare_kg']) if tare else None
        cargo_gross = terminal - tare_kg if None not in (terminal, tare_kg) else None
        delta = cargo_gross - gross if cargo_gross is not None else None
        weighed_seal = weigh.get('weighed_seal', '') if weigh else ''
        original_seal = seal.get('seal', '') if seal else ''
        seal_ok = bool(weigh and seal) and weighed_seal == original_seal

        # 结构性阻断：与重量口径无关，换口径也消不掉。
        structural = []
        for node in unrooted_here:
            structural.append({'code': 'UNROOTED', 'detail': f'{node} 无法追溯到主单，不能计入交接'})
        for error in graph_errors:
            if container_of.get(error.get('id', '')) == container and error['code'] != 'MISSING_PARENT':
                structural.append({'code': error['code'],
                                   'detail': f"关系错误 {error.get('path', error['id'])}"})
        for check in weight_checks:
            if check['code'] != 'BALANCED' and check['id'] in node_set:
                structural.append({'code': 'SUBTREE_MISMATCH',
                                   'detail': f"{check['id']} {check['field']} 差 {check['difference']}"})
        for issue in hazard_issues:
            if issue.get('id') in node_set or issue.get('id') == container:
                structural.append({'code': issue['code'], 'detail': issue.get('classes', issue['code'])})

        # 证据阻断：说明称重口径本身还不能成立。
        evidence_blockers = []
        if not nodes and not unrooted_here:
            evidence_blockers.append({'code': 'NO_MANIFEST_CARGO', 'detail': '只有码头记录，没有可交接的舱单货'})
        if not weigh:
            evidence_blockers.append({'code': 'NO_WEIGHING', 'detail': '无称重结果，待地磅回传'})
        if not tare:
            evidence_blockers.append({'code': 'NO_TARE', 'detail': '缺皮重记录，无法从整箱毛重还原货重'})
        if not seal:
            evidence_blockers.append({'code': 'NO_SEAL_LEDGER', 'detail': '缺原申报封签记录'})
        if cargo_gross is not None and cargo_gross < 0:
            evidence_blockers.append({'code': 'NEGATIVE_CARGO', 'detail': '整箱毛重低于皮重'})
        if weigh and seal and not seal_ok:
            evidence_blockers.append({'code': 'SEAL_MISMATCH',
                                      'detail': f'称重封签 {weighed_seal} 与申报封签 {original_seal} 不一致'})
        weight_delta_blocker = None
        if delta is not None and abs(delta) > tolerance:
            weight_delta_blocker = {'code': 'WEIGHT_DELTA',
                                    'detail': f'称重与申报货毛重差 {_fmt(delta)} kg，未说明前保留原申报'}

        can_weigh = (bool(weigh and tare and seal) and cargo_gross is not None
                     and cargo_gross >= 0 and seal_ok and not unrooted_here and not structural)

        instruction = requested.get(container)
        if instruction is not None:
            if instruction['basis'] == BASIS_WEIGHING:
                if not can_weigh:
                    reason = '；'.join(item['detail'] for item in evidence_blockers) or '称重不可复核'
                    raise BusinessError('BASIS_UNVERIFIABLE', f'{container} 不能切换到称重口径：{reason}')
                if weight_delta_blocker and not instruction['note']:
                    raise BusinessError('DIFFERENCE_UNEXPLAINED',
                                        f'{container} 称重与申报差 {_fmt(delta)} kg，须先写明差异说明')
            basis = instruction['basis']
            note = instruction['note']
        else:
            note = ''
            if can_weigh and weight_delta_blocker is None:
                basis = BASIS_WEIGHING
            else:
                basis = BASIS_DECLARED

        blockers = structural + evidence_blockers
        if basis == BASIS_DECLARED and weight_delta_blocker:
            # 保留申报口径时，称重与申报的差异仍是未处理项，挂阻断等说明。
            blockers.append(weight_delta_blocker)
        if basis == BASIS_WEIGHING and weight_delta_blocker:
            # 单证员已写明差异说明，改按称重口径交接；差异值仍留在单上可复核。
            note = note or '差异已说明'
        if not blockers:
            verdict = '可交接'
        elif all(item['code'] == 'NO_WEIGHING' for item in blockers):
            verdict = '待称重'
        else:
            verdict = '阻断'

        if note:
            basis_note = note
        elif basis == BASIS_WEIGHING:
            basis_note = BASIS_NOTE[basis]
        elif not weigh:
            basis_note = '保留原申报货重，待地磅结果'
        elif not tare:
            basis_note = DECLARED_NOTE['NO_TARE']
        elif not seal:
            basis_note = DECLARED_NOTE['NO_SEAL_LEDGER']
        elif weigh and seal and not seal_ok:
            basis_note = DECLARED_NOTE['SEAL_MISMATCH']
        elif unrooted_here:
            basis_note = DECLARED_NOTE['UNROOTED']
        elif weight_delta_blocker:
            basis_note = DECLARED_NOTE['WEIGHT_DELTA']
        elif structural:
            basis_note = '保留原申报货重，' + structural[0]['code']
        else:
            basis_note = BASIS_NOTE[basis]

        seen = set()
        unique = []
        for item in blockers:
            marker = (item['code'], item['detail'])
            if marker not in seen:
                seen.add(marker)
                unique.append(item)
        blockers = unique

        rows.append({
            'sequence': 0,
            'container': container,
            'seal': original_seal,
            'seal_source': seal.get('source', '') if seal else '',
            'weighed_seal': weighed_seal,
            'terminal_gross_kg': _fmt(terminal) if terminal is not None else '',
            'tare_kg': _fmt(tare_kg) if tare_kg is not None else '',
            'cargo_gross_kg': _fmt(cargo_gross) if cargo_gross is not None else '',
            'manifest_net_kg': _fmt(net),
            'manifest_gross_kg': _fmt(gross),
            'declared_gross_kg': _fmt(root_gross),
            'delta_kg': _fmt(delta) if delta is not None else '',
            'pieces': pieces,
            'node_count': len(nodes),
            'chain_before': ','.join(root_ids) or '（无主单）',
            'chain_after': f"{container}:{','.join(leaves) or '（无箱内货）'}",
            'basis': BASIS_LABEL[basis],
            'basis_code': basis,
            'basis_note': basis_note,
            'seal_match': ('一致' if seal_ok else '不一致') if weigh and seal else '未核',
            'weighed': '是' if weigh else '否',
            'weighed_at': weigh.get('weighed_at', '') if weigh else '',
            'verdict': verdict,
            'can_switch_weighing': can_weigh,
            'blocker': '；'.join(item['detail'] for item in blockers) or '无',
            'evidence': _evidence_text(weigh, tare, seal, names) or '（无证据）',
            '_blockers': [item['code'] for item in blockers],
            '_node_ids': nodes,
        })

    # 船期：先交已有称重结果且无阻断的箱，再交已有称重但待说明的箱，最后才是待称重。
    def _order(row):
        weighed = 0 if row['weighed'] == '是' else 1
        ready = 0 if row['verdict'] == '可交接' else 1
        return (weighed, ready, row['weighed_at'], row['container'])

    rows.sort(key=_order)
    for index, row in enumerate(rows, 1):
        row['sequence'] = index

    root_rows = [by_id[root] for root in roots]
    leaf_rows = [by_id[node] for node in sorted(reachable) if not children.get(node)]
    before = {'roots': len(roots), 'pieces': sum(row['pieces'] for row in root_rows),
              'net_kg': _fmt(_sum(root_rows, 'net_kg')), 'gross_kg': _fmt(_sum(root_rows, 'gross_kg'))}
    after = {'containers': len(rows), 'leaves': len(leaf_rows),
             'pieces': sum(row['pieces'] for row in leaf_rows),
             'net_kg': _fmt(_sum(leaf_rows, 'net_kg')), 'gross_kg': _fmt(_sum(leaf_rows, 'gross_kg'))}
    difference = {'pieces': after['pieces'] - before['pieces'],
                  'net_kg': _fmt(Decimal(after['net_kg']) - Decimal(before['net_kg'])),
                  'gross_kg': _fmt(Decimal(after['gross_kg']) - Decimal(before['gross_kg']))}

    owners = {}
    for container, node_ids in assigned.items():
        for node in node_ids:
            owners.setdefault(node, []).append(container)
    duplicated = sorted(node for node, holders in owners.items() if len(holders) > 1)
    # 箱合计只取叶节点，且每个可达叶节点只落进一个箱：拆并前后不会把祖先再算一遍。
    # 无主单节点被排除在箱合计之外（不冒充已计），但仍单独列出并阻断封存。
    counted_once = not duplicated and not no_container
    reconciled = counted_once and difference == {'pieces': 0, 'net_kg': '0', 'gross_kg': '0'}
    conservation = {
        'before': before,
        'after': after,
        'difference': difference,
        'counted_once': counted_once,
        'reconciled': reconciled,
        'duplicated': sorted({node for node in duplicated}
                             | {container for node, holders in owners.items() if len(holders) > 1
                                for container in holders}),
        'unrooted': unrooted,
        'missing_container': no_container,
        'evidence_rejected': rejected,
        'tolerance_kg': _fmt(tolerance),
        'policy': '称重可复核且逐箱对得上时采用称重口径；否则保留原申报货重并阻断，差异说明后才改口径。',
    }
    ready = all(row['verdict'] == '可交接' for row in rows) and counted_once and not rejected
    return {'rows': rows, 'conservation': conservation, 'ready': ready,
            'blocked': sum(row['verdict'] == '阻断' for row in rows),
            'pending': sum(row['verdict'] == '待称重' for row in rows),
            'evidence_meta': metas, 'evidence_rejected': rejected,
            'instructions': {'switches': [{'container': key, **requested[key]} for key in sorted(requested)]}
            if requested else {}}


def apply(state):
    sheet = build(state, state.get('handover_instructions'))
    state['handover'] = sheet
    state['views']['handover'] = {
        'title': '箱门重量交接单',
        'rows': sheet['rows'],
        'metrics': {'出港箱': len(sheet['rows']),
                    '可交接': sum(row['verdict'] == '可交接' for row in sheet['rows']),
                    '待称重': sheet['pending'],
                    '阻断': sheet['blocked'],
                    '只计一次': '是' if sheet['conservation']['counted_once'] else '否'},
    }
