import sqlite3, json, hashlib
from pathlib import Path
from datetime import datetime, timezone
from app.errors import BusinessError

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核', version_no INTEGER NOT NULL DEFAULT 1, instruction_digest TEXT)",
    "CREATE TABLE IF NOT EXISTS run_versions (run_id TEXT NOT NULL, version_no INTEGER NOT NULL, instruction_digest TEXT NOT NULL, instruction TEXT NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核', created_at TEXT NOT NULL, PRIMARY KEY (run_id, version_no), UNIQUE (run_id, instruction_digest))",
]


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    for statement in SCHEMA:
        con.execute(statement)
    columns = {row['name'] for row in con.execute('PRAGMA table_info(runs)')}
    if 'version_no' not in columns:
        con.execute('ALTER TABLE runs ADD COLUMN version_no INTEGER NOT NULL DEFAULT 1')
    if 'instruction_digest' not in columns:
        con.execute('ALTER TABLE runs ADD COLUMN instruction_digest TEXT')
    return con


def instruction_digest(instruction):
    payload = json.dumps(instruction or {}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _register(con, run_id, digest, body, instructions, marker):
    """追加批次内版本；同一 instruction 摘要幂等复用，不重复登记。

    仅由 switch 路径调用；创建批次走 ensure，绝不覆盖已有批次的当前版本。
    """
    current = con.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
    if current is None:
        con.execute('INSERT INTO runs(id,digest,result,revision,status,version_no,instruction_digest) VALUES(?,?,?,?,?,?,?)',
                    (run_id, digest, body, 1, '待复核', 1, marker))
        con.execute('INSERT INTO run_versions(run_id,version_no,instruction_digest,instruction,result,revision,status,created_at) VALUES(?,?,?,?,?,?,?,?)',
                    (run_id, 1, marker, json.dumps(instructions, ensure_ascii=False, sort_keys=True),
                     body, 1, '待复核', _now()))
        return
    if current['instruction_digest'] == marker:
        # 同一口径（含服务重启后重建）不新增版本，也不推进修订号。
        return
    existing = con.execute('SELECT * FROM run_versions WHERE run_id=? AND instruction_digest=?',
                           (run_id, marker)).fetchone()
    if existing:
        # 回到曾经用过的同一口径：复用已登记版本，不新增。
        con.execute('UPDATE runs SET result=?, revision=revision+1, status=?, version_no=?, instruction_digest=? WHERE id=?',
                    (existing['result'], existing['status'], existing['version_no'], marker, run_id))
        return
    next_no = con.execute('SELECT COALESCE(MAX(version_no),0)+1 FROM run_versions WHERE run_id=?',
                          (run_id,)).fetchone()[0]
    con.execute('INSERT INTO run_versions(run_id,version_no,instruction_digest,instruction,result,revision,status,created_at) VALUES(?,?,?,?,?,?,?,?)',
                (run_id, next_no, marker, json.dumps(instructions, ensure_ascii=False, sort_keys=True),
                 body, 1, '待复核', _now()))
    con.execute('UPDATE runs SET result=?, revision=revision+1, status=?, version_no=?, instruction_digest=? WHERE id=?',
                (body, '待复核', next_no, marker, run_id))


def ensure(path, result):
    """启动时确保默认批次存在；已存在则原样保留，绝不回退用户切换过的版本。"""
    run_id = result['manifest']['run_id']
    digest = result['manifest']['sha256']
    instructions = (result.get('input_snapshot') or {}).get('handover') or {}
    marker = instruction_digest(instructions)
    body = json.dumps(result, ensure_ascii=False)
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result,revision,status,version_no,instruction_digest) VALUES(?,?,?,?,?,?,?)',
                    (run_id, digest, body, 1, '待复核', 1, marker))
        con.execute('INSERT OR IGNORE INTO run_versions(run_id,version_no,instruction_digest,instruction,result,revision,status,created_at) VALUES(?,?,?,?,?,?,?,?)',
                    (run_id, 1, marker, json.dumps(instructions, ensure_ascii=False, sort_keys=True),
                     body, 1, '待复核', _now()))


def save(path, result):
    """登记批次：同输入只保留一条，重复提交原样返回，绝不回退已切换的版本。"""
    ensure(path, result)
    return get(path, result['manifest']['run_id'])


def get(path, run_id):
    with connect(path) as con:
        row = con.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
    if not row:
        raise BusinessError('NOT_FOUND', '批次不存在', 404)
    result = json.loads(row['result'])
    result.update(revision=row['revision'], status=row['status'], version_no=row['version_no'])
    return result


def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status,version_no FROM runs ORDER BY rowid DESC LIMIT 100')]


def versions(path, run_id):
    with connect(path) as con:
        rows = list(con.execute('SELECT * FROM run_versions WHERE run_id=? ORDER BY version_no DESC', (run_id,)))
        exists = con.execute('SELECT 1 FROM runs WHERE id=?', (run_id,)).fetchone()
    if not rows and not exists:
        raise BusinessError('NOT_FOUND', '批次不存在', 404)
    return [{'version_no': row['version_no'], 'instruction_digest': row['instruction_digest'],
             'instruction': json.loads(row['instruction']), 'revision': row['revision'],
             'status': row['status'], 'created_at': row['created_at']} for row in rows]


def switch(path, run_id, instructions, result, expected_revision=None):
    """切换口径：result 必须已在事务外重算并校验通过。

    同一指令重试直接返回当前批次，不新增版本；换新指令则追加版本。
    写入 runs 指针与 run_versions 记录在同一事务内完成，失败整体回滚，
    旧版与快照中的原始封签保持可读。
    """
    marker = instruction_digest(instructions)
    body = json.dumps(result, ensure_ascii=False)
    with connect(path) as con:
        current = con.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if not current:
            raise BusinessError('NOT_FOUND', '批次不存在', 404)
        if current['instruction_digest'] == marker:
            return get(path, run_id)
        if expected_revision is not None and current['revision'] != expected_revision:
            raise BusinessError('STALE_VERSION', '复核版本已更新，请刷新', 409)
        digest = current['digest']
        _register(con, run_id, digest, body, instructions, marker)
    return get(path, run_id)


def restore(path, run_id, version_no):
    """恢复到指定历史版本；只移动 runs 指针，历史版本不删除，封签随快照保留。"""
    with connect(path) as con:
        row = con.execute('SELECT * FROM run_versions WHERE run_id=? AND version_no=?',
                          (run_id, version_no)).fetchone()
        if not row:
            if not con.execute('SELECT 1 FROM runs WHERE id=?', (run_id,)).fetchone():
                raise BusinessError('NOT_FOUND', '批次不存在', 404)
            raise BusinessError('VERSION_NOT_FOUND', '版本不存在', 404)
        con.execute('UPDATE runs SET result=?, revision=revision+1, status=?, version_no=?, instruction_digest=? WHERE id=?',
                    (row['result'], row['status'], row['version_no'], row['instruction_digest'], run_id))
    return get(path, run_id)


def review(path, run_id, revision):
    with connect(path) as con:
        cur = con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',
                          ('已复核', run_id, revision))
        if cur.rowcount != 1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?', (run_id,)).fetchone():
                raise BusinessError('NOT_FOUND', '批次不存在', 404)
            raise BusinessError('STALE_VERSION', '复核版本已更新，请刷新', 409)
        row = con.execute('SELECT version_no FROM runs WHERE id=?', (run_id,)).fetchone()
        con.execute('UPDATE run_versions SET revision=revision+1,status=? WHERE run_id=? AND version_no=?',
                    ('已复核', run_id, row['version_no']))
    return get(path, run_id)
