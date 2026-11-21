"""箱门重量交接：逐箱证据、口径选择、守恒与切换回退。"""
from copy import deepcopy
from decimal import Decimal

import pytest

from app.engine import evaluate, merge_instructions, normalize_instructions
from app.errors import BusinessError
from app.modules.handover.rules import build, read_evidence, WEIGH_FIELDS, SEAL_FIELDS, TARE_FIELDS


def sheet(dataset='dirty'):
    return {row['container']: row for row in evaluate({'dataset': dataset})['handover']['rows']}


def test_clean_all_handover(clean):
    rows = clean['handover']['rows']
    assert len(rows) == 6
    assert all(row['verdict'] == '可交接' for row in rows)
    assert clean['handover']['ready'] is True


def test_terminal_gross_minus_tare(sample):
    row = sheet()['C001']
    assert row['terminal_gross_kg'] == '2641'
    assert row['tare_kg'] == '2200'
    assert row['cargo_gross_kg'] == '441'
    assert row['delta_kg'] == '1'


def test_dirty_delta_keeps_declared(sample):
    # 1kg 毛重差异未说明，保留原申报口径并阻断，不静默改成称重值。
    row = sheet()['C004']
    assert row['verdict'] == '阻断'
    assert row['basis_code'] == 'declared'
    assert row['manifest_gross_kg'] == '440'
    assert 'WEIGHT_DELTA' in row['_blockers']


def test_seal_ledger_wins(sample):
    # 称重时读到的封签与申报台账不一致：两者并列显示，申报封签不被覆盖。
    row = sheet()['C003']
    assert row['seal'] == 'SL-C003-02'
    assert row['weighed_seal'] == 'SL-C003-01'
    assert row['seal_match'] == '不一致'
    assert 'SEAL_MISMATCH' in row['_blockers']


def test_weighing_sequence_ship_first(sample):
    rows = sample['handover']['rows']
    weighed = [row for row in rows if row['weighed'] == '是']
    waiting = [row for row in rows if row['weighed'] == '否']
    assert [row['sequence'] for row in weighed] == list(range(1, len(weighed) + 1))
    assert waiting and all(row['sequence'] > len(weighed) for row in waiting)
    assert waiting[0]['verdict'] == '待称重'


def test_conservation_counts_once(clean):
    conservation = clean['handover']['conservation']
    assert conservation['counted_once'] is True
    assert conservation['reconciled'] is True
    assert conservation['before']['net_kg'] == conservation['after']['net_kg'] == '2400'
    assert conservation['before']['gross_kg'] == conservation['after']['gross_kg'] == '2640'


def test_dirty_unrooted_not_counted(sample):
    conservation = sample['handover']['conservation']
    assert conservation['unrooted'] == ['LOOP-A', 'LOOP-B', 'ORPHAN']
    # 无主单节点不进入拆并后的箱内合计。
    assert conservation['after']['net_kg'] == '2400'
    assert conservation['after']['gross_kg'] == '2640'


def test_internal_nodes_not_double_counted(clean):
    # 拆并后只取叶节点：中间层 M001-P1 的申报量不再加一遍。
    conservation = clean['handover']['conservation']
    assert conservation['after']['leaves'] == 24
    assert conservation['after']['pieces'] == 240
    assert conservation['after']['pieces'] != sum(row['pieces'] for row in clean['records'])


def test_split_box_still_once(clean):
    # 把一个中间层单放在另一箱：叶量仍各计一次，前后合计不变。
    state = deepcopy(clean)
    target = next(row for row in state['records'] if row['id'] == 'M001-P1')
    target['container'] = 'C009'
    result = build(state)
    assert result['conservation']['counted_once'] is True
    assert result['conservation']['difference'] == {'pieces': 0, 'net_kg': '0', 'gross_kg': '0'}
    assert any(row['container'] == 'C009' for row in result['rows'])


def test_blank_container_blocks_once(clean):
    state = deepcopy(clean)
    next(row for row in state['records'] if row['id'] == 'M001-P1-1')['container'] = ''
    result = build(state)
    assert result['conservation']['counted_once'] is False
    assert result['conservation']['missing_container'] == ['M001-P1-1']


def test_orphan_container_flagged(sample):
    row = sheet()['Y']
    assert row['verdict'] == '阻断'
    assert 'UNROOTED' in row['_blockers']


def test_missing_evidence_is_explicit(clean):
    state = deepcopy(clean)
    state['evidence'] = {'weighing': b'', 'tare': b'', 'seal': b'', 'names': {'weighing': 'w', 'tare': 't', 'seal': 's'}}
    result = build(state)
    assert all(row['verdict'] == '阻断' for row in result['rows'])
    assert any(item['code'] == 'NO_EVIDENCE' for item in result['evidence_rejected'])


def test_evidence_duplicate_conflict_rejected(clean):
    raw = ('container,seal,source,revision\nC001,A,台账,1\nC001,B,台账,1\n').encode()
    indexed, rejected, meta, hard = read_evidence(raw, 'seal', SEAL_FIELDS)
    assert 'C001' not in indexed
    assert any(item['code'] == 'DUPLICATE_CONFLICT' for item in rejected)


def test_evidence_legacy_encoding():
    raw = ('container,tare,weight_unit,source,revision\nC001,2200,kg,皮重台账,1\n').encode('gb18030')
    indexed, rejected, meta, hard = read_evidence(raw, 'tare', TARE_FIELDS)
    assert meta['encoding'] == 'gb18030'
    assert indexed['C001']['tare_kg'] == '2200'


def test_switch_to_weighing_with_note(clean):
    state = deepcopy(clean)
    result = build(state, {'switches': [{'container': 'C001', 'basis': 'weighing'}]})
    row = next(item for item in result['rows'] if item['container'] == 'C001')
    assert row['basis_code'] == 'weighing'
    assert row['verdict'] == '可交接'
    assert result['instructions']['switches'][0]['container'] == 'C001'


def test_switch_unverifiable_raises(clean):
    state = deepcopy(clean)
    state['evidence'] = {'weighing': b'', 'tare': b'', 'seal': b'', 'names': {'weighing': 'w', 'tare': 't', 'seal': 's'}}
    with pytest.raises(BusinessError) as error:
        build(state, {'switches': [{'container': 'C001', 'basis': 'weighing'}]})
    assert error.value.code == 'BASIS_UNVERIFIABLE'


def test_switch_delta_requires_note(clean):
    import base64
    state = deepcopy(clean)
    snapshot = state['input_snapshot']
    raw = base64.b64decode(snapshot['evidence_b64']['weighing']).replace(b'C001,2640', b'C001,2660')
    snapshot['evidence_b64']['weighing'] = base64.b64encode(raw).decode()
    with pytest.raises(BusinessError) as error:
        build(state, {'switches': [{'container': 'C001', 'basis': 'weighing'}]})
    assert error.value.code == 'DIFFERENCE_UNEXPLAINED'
    result = build(state, {'switches': [{'container': 'C001', 'basis': 'weighing', 'note': '复磅'}]})
    row = next(item for item in result['rows'] if item['container'] == 'C001')
    assert row['basis_code'] == 'weighing'
    assert row['delta_kg'] == '20'


def test_switch_structural_blocks_weighing(clean):
    state = deepcopy(clean)
    state['hazard_issues'] = [{'id': 'M001-P1', 'code': 'INCOMPATIBLE', 'classes': '3/5.1'}]
    with pytest.raises(BusinessError) as error:
        build(state, {'switches': [{'container': 'C001', 'basis': 'weighing'}]})
    assert error.value.code == 'BASIS_UNVERIFIABLE'


def test_switch_unknown_container(clean):
    with pytest.raises(BusinessError) as error:
        build(deepcopy(clean), {'switches': [{'container': 'NOPE', 'basis': 'weighing'}]})
    assert error.value.code == 'UNKNOWN_REFERENCE'


def test_instruction_conflict_same_container(clean):
    with pytest.raises(BusinessError) as error:
        build(deepcopy(clean), {'switches': [{'container': 'C001', 'basis': 'weighing'},
                                            {'container': 'C001', 'basis': 'declared'}]})
    assert error.value.code == 'BAD_INSTRUCTION'


def test_instruction_order_invariant(clean):
    a = build(deepcopy(clean), {'switches': [{'container': 'C001', 'basis': 'declared'},
                                            {'container': 'C002', 'basis': 'declared'}]})
    b = build(deepcopy(clean), {'switches': [{'container': 'C002', 'basis': 'declared'},
                                            {'container': 'C001', 'basis': 'declared'}]})
    assert a['rows'] == b['rows']
    assert normalize_instructions({'switches': [{'container': 'B', 'basis': 'declared'},
                                                {'container': 'A', 'basis': 'declared'}]}) == \
        normalize_instructions({'switches': [{'container': 'A', 'basis': 'declared'},
                                            {'container': 'B', 'basis': 'declared'}]})


def test_merge_keeps_unmentioned(clean):
    first = {'switches': [{'container': 'C001', 'basis': 'declared'}]}
    merged = merge_instructions(first, {'switches': [{'container': 'C002', 'basis': 'declared'}]})
    assert [item['container'] for item in merged['switches']] == ['C001', 'C002']
    replaced = merge_instructions(merged, {'switches': [{'container': 'C001', 'basis': 'weighing'}]})
    assert {item['container']: item['basis'] for item in replaced['switches']}['C001'] == 'weighing'
    assert merge_instructions(merged, {}) == merged


def test_archive_consumes_handover(sample, clean):
    assert clean['archive']['status'] == '可封存'
    assert clean['archive']['handover_ready'] is True
    assert sample['archive']['status'] == '待处理'
    assert sample['archive']['handover_blocked'] >= 1
    assert sample['archive']['handover_counted_once'] is True


def test_archive_without_evidence_not_gated():
    # 纯 CSV 导入没有箱门证据，交接不冒充封存前提。
    from app.config import ROOT
    state = evaluate({'csv_text': (ROOT / 'sample_data/clean.csv').read_text()})
    assert state['archive']['handover_applicable'] is False
    assert state['archive']['status'] == '可封存'
