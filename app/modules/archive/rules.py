import hashlib,json

def _handover_gate(state):
    sheet=state.get('handover') or {}
    conservation=sheet.get('conservation') or {}
    metas=sheet.get('evidence_meta') or {}
    rows=sheet.get('rows') or []
    # 没有提供任何称重/皮重/封签证据时（例如纯 CSV 导入），交接不是本批的封存前提；
    # 一旦提供了证据，就必须逐箱可交接且件净毛只计一次才能封存。
    applicable=bool(state.get('evidence_source')) or any((meta or {}).get('rows') for meta in metas.values())
    counted_once=bool(conservation.get('counted_once',False))
    blocked=sum(row.get('verdict')!='可交接' for row in rows)
    ready=bool(rows) and not blocked and counted_once and not conservation.get('evidence_rejected')
    return {'applicable':applicable,'blocked':blocked,'counted_once':counted_once,
            'reconciled':bool(conservation.get('reconciled',False)),'ready':ready}

def decide(state):
    b=state['balance']
    core=b['graph_errors']+b['weight_errors']+len(b['declaration_differences'])+len(state['hazard_issues'])+len(state['quarantine'])
    handover=_handover_gate(state)
    handover_ready=(not handover['applicable']) or handover['ready']
    evidence={'roots':state['graph']['roots'],'weights':state['weight_checks'],'hazards':state['hazard_issues'],
              'handover':{key:value for key,value in (state.get('handover') or {}).items() if key!='rows'},
              'handover_rows':[{'container':row['container'],'seal':row['seal'],'verdict':row['verdict'],
                                'basis':row['basis'],'delta_kg':row['delta_kg']}
                               for row in (state.get('handover') or {}).get('rows',[])]}
    return {'status':'可封存' if b['row_balanced'] and not core and handover_ready else '待处理',
            'blocked':core,
            'handover_applicable':handover['applicable'],
            'handover_blocked':handover['blocked'],
            'handover_pending':(state.get('handover') or {}).get('pending',0),
            'handover_counted_once':handover['counted_once'],
            'handover_reconciled':handover['reconciled'],
            'handover_ready':handover['ready'],
            'total_blocked':core+handover['blocked']+(0 if handover['counted_once'] else 1),
            'seal':hashlib.sha256(json.dumps(evidence,sort_keys=True,default=str).encode()).hexdigest()}
def apply(state):
    state['archive']=decide(state)
    state['views']['archive']={'title':'版本封存','rows':[{'id':r['id'],'revision':r['revision'],'parent':r['parent']} for r in state['records']],'metrics':{'主单':len(state['graph']['roots']),'状态':state['archive']['status'],'阻断':state['archive']['blocked'],'交接阻断':state['archive']['handover_blocked'],'待称重':state['archive']['handover_pending'],'只计一次':'是' if state['archive']['handover_counted_once'] else '否'}}
