"""交接指令的 API 行为：幂等重试、陈旧版本、失败回退与封签保全。"""
from fastapi.testclient import TestClient

from app import main


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'handover.sqlite'))
    return TestClient(main.app)


def dirty_run(c):
    response = c.post('/api/runs', json={'dataset': 'dirty'})
    assert response.status_code == 200
    return response.json()


def test_eight_entries(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    for key in main.MODULES:
        assert c.post('/api/' + key, json={}).status_code == 200
        assert c.get('/' + key).status_code == 200
    assert 'handover' in main.MODULES


def test_same_instruction_idempotent(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = dirty_run(c)
    run_id = run['manifest']['run_id']
    instruction = {'handover': {'switches': [{'container': 'C002', 'basis': 'declared'}]}}
    first = c.post(f'/api/runs/{run_id}/switch', json=instruction)
    assert first.status_code == 200
    second = c.post(f'/api/runs/{run_id}/switch', json=instruction)
    assert second.status_code == 200
    assert second.json()['version_no'] == first.json()['version_no']
    assert second.json()['revision'] == first.json()['revision']
    assert len(c.get(f'/api/runs/{run_id}/versions').json()) == 2
    assert len(c.get('/api/runs').json()) == 1


def test_switch_reject_leaves_old_version(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = dirty_run(c)
    run_id = run['manifest']['run_id']
    before = c.get(f'/api/runs/{run_id}').json()
    response = c.post(f'/api/runs/{run_id}/switch',
                      json={'handover': {'switches': [{'container': 'C005', 'basis': 'weighing'}]}})
    assert response.status_code == 422
    assert response.json()['code'] == 'BASIS_UNVERIFIABLE'
    after = c.get(f'/api/runs/{run_id}').json()
    assert after['version_no'] == before['version_no']
    assert after['revision'] == before['revision']
    assert after['handover']['rows'] == before['handover']['rows']
    assert len(c.get(f'/api/runs/{run_id}/versions').json()) == 1


def test_failed_switch_keeps_original_seal(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = dirty_run(c)
    run_id = run['manifest']['run_id']
    before = {row['container']: row for row in run['handover']['rows']}
    failed = c.post(f'/api/runs/{run_id}/switch',
                    json={'handover': {'switches': [{'container': 'C001', 'basis': 'weighing', 'note': '复磅'}]}})
    assert failed.status_code == 422
    current = {row['container']: row for row in c.get(f'/api/runs/{run_id}').json()['handover']['rows']}
    assert current['C001']['seal'] == before['C001']['seal'] == 'SL-C001-01'
    assert current['C003']['seal'] == before['C003']['seal'] == 'SL-C003-02'
    assert current['C003']['weighed_seal'] == 'SL-C003-01'


def test_switch_and_restore_roundtrip(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = dirty_run(c)
    run_id = run['manifest']['run_id']
    switched = c.post(f'/api/runs/{run_id}/switch',
                      json={'handover': {'switches': [{'container': 'C004', 'basis': 'weighing', 'note': '码头复磅'}]}})
    assert switched.status_code == 200
    body = switched.json()
    assert body['version_no'] == 2
    row = next(item for item in body['handover']['rows'] if item['container'] == 'C004')
    assert row['basis_code'] == 'weighing'
    assert row['verdict'] == '可交接'
    assert row['delta_kg'] == '20'
    restored = c.post(f'/api/runs/{run_id}/restore', json={'version_no': 1})
    assert restored.status_code == 200
    assert restored.json()['version_no'] == 1
    old = next(item for item in restored.json()['handover']['rows'] if item['container'] == 'C004')
    assert old['basis_code'] == 'declared'
    assert old['seal'] == 'SL-C004-01'


def test_stale_switch_revision(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = dirty_run(c)
    run_id = run['manifest']['run_id']
    first = c.post(f'/api/runs/{run_id}/switch',
                   json={'revision': run['revision'],
                         'handover': {'switches': [{'container': 'C002', 'basis': 'declared'}]}})
    assert first.status_code == 200
    stale = c.post(f'/api/runs/{run_id}/switch',
                   json={'revision': run['revision'],
                         'handover': {'switches': [{'container': 'C002', 'basis': 'weighing'}]}})
    assert stale.status_code == 409
    assert stale.json()['code'] == 'STALE_VERSION'


def test_compact_switch_keeps_unmentioned(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = dirty_run(c)
    run_id = run['manifest']['run_id']
    c.post(f'/api/runs/{run_id}/switch', json={'handover': {'switches': [{'container': 'C002', 'basis': 'declared'}]}})
    second = c.post(f'/api/runs/{run_id}/switch', json={'handover': {'switches': [{'container': 'C004', 'basis': 'weighing', 'note': '复磅'}]}})
    assert second.status_code == 200
    rows = {row['container']: row for row in second.json()['handover']['rows']}
    assert rows['C002']['basis_code'] == 'declared'
    assert rows['C004']['basis_code'] == 'weighing'
    assert len(second.json()['handover']['instructions']['switches']) == 2


def test_bad_instruction_shapes(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = dirty_run(c)
    run_id = run['manifest']['run_id']
    assert c.post(f'/api/runs/{run_id}/switch', json={'handover': {'switches': [{'container': 'C001', 'basis': 'ton'}]}}).status_code == 422
    assert c.post(f'/api/runs/{run_id}/switch', json={'handover': {'switches': [{'basis': 'weighing'}]}}).status_code == 422
    assert c.post(f'/api/runs/{run_id}/switch', json={'handover': {'switches': [{'container': 'NOPE', 'basis': 'weighing'}]}}).status_code == 422
    assert c.post(f'/api/runs/{run_id}/switch', json={'handover': 'bad'}).status_code == 422


def test_export_includes_handover(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = dirty_run(c)
    run_id = run['manifest']['run_id']
    import io, json, zipfile
    archive = zipfile.ZipFile(io.BytesIO(c.get(f'/api/export/{run_id}').content))
    evidence = json.loads(archive.read('evidence.json'))
    assert evidence['views']['handover']['title'] == '箱门重量交接单'
    assert len(evidence['views']['handover']['rows']) == len(run['handover']['rows'])


def test_handover_page_marks_blockers(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = dirty_run(c)
    run_id = run['manifest']['run_id']
    html = c.get('/handover?run_id=' + run_id).text
    assert '箱门重量交接单' in html
    assert '只计一次' in html
    assert 'SL-C003-02' in html and 'SL-C003-01' in html


def test_rerun_does_not_roll_back_switch(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = dirty_run(c)
    run_id = run['manifest']['run_id']
    c.post(f'/api/runs/{run_id}/switch',
           json={'handover': {'switches': [{'container': 'C004', 'basis': 'weighing', 'note': '复磅'}]}})
    rerun = c.post(f'/api/runs/{run_id}/rerun', json={})
    assert rerun.status_code == 200
    assert rerun.json()['version_no'] == 2
    row = next(item for item in rerun.json()['handover']['rows'] if item['container'] == 'C004')
    assert row['basis_code'] == 'weighing'


def test_restart_preserves_switch(tmp_path, monkeypatch):
    path = tmp_path / 'restart.sqlite'
    monkeypatch.setattr(main, 'DB_PATH', str(path))
    with TestClient(main.app) as first:
        run = first.post('/api/runs', json={'dataset': 'dirty'}).json()
        run_id = run['manifest']['run_id']
        first.post(f'/api/runs/{run_id}/switch',
                   json={'handover': {'switches': [{'container': 'C004', 'basis': 'weighing', 'note': '复磅'}]}})
    with TestClient(main.app) as second:
        current = second.get(f'/api/runs/{run_id}').json()
    assert current['version_no'] == 2
    row = next(item for item in current['handover']['rows'] if item['container'] == 'C004')
    assert row['basis_code'] == 'weighing'
    assert row['seal'] == 'SL-C004-01'


def test_switching_back_reuses_version(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = dirty_run(c)
    run_id = run['manifest']['run_id']
    c.post(f'/api/runs/{run_id}/switch', json={'handover': {'switches': [{'container': 'C002', 'basis': 'declared'}]}})
    back = c.post(f'/api/runs/{run_id}/switch', json={'handover': {'switches': [{'container': 'C002', 'basis': 'weighing'}]}})
    assert back.status_code == 200
    assert back.json()['version_no'] == 3
    # 空指令是“不改决定”的 no-op，不新增版本。
    noop = c.post(f'/api/runs/{run_id}/switch', json={'handover': {'switches': []}})
    assert noop.status_code == 200
    assert noop.json()['version_no'] == 3
    assert len(c.get(f'/api/runs/{run_id}/versions').json()) == 3
