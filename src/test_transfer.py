import json
import shutil
import tempfile
from pathlib import Path

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

import data_store
import graph
import functions
from graph import BankState, TransferAccounts, build_transfer_graph

# 실제 data.json은 건드리지 않고 원본의 임시 복사본으로만 테스트한다
TMP_DIR = Path(tempfile.mkdtemp())
data_store.DATA_PATH = TMP_DIR / 'data.json'


def fresh():
    shutil.copyfile(data_store.INITIAL_PATH, data_store.DATA_PATH)


def raw():
    return data_store.DATA_PATH.read_bytes()


def bal(account_id):
    return next(a['balance'] for a in data_store.load_data()['accounts'] if a['account_id'] == account_id)


def check(name, cond, detail=''):
    assert cond, f'FAIL - {name} {detail}'
    print('PASS -', name, detail)


OWNER = 'user-001'

# ---------- 1. 검증 함수 ----------
fresh()
d = data_store.load_data()
v = lambda f, t, a: functions.validate_transfer(d, OWNER, f, t, a)
check('정상 이체는 통과', v('acc-002', 'acc-003', 500000) is None)
check('금액 0 거부', v('acc-002', 'acc-003', 0) is not None)
check('금액 음수 거부', v('acc-002', 'acc-003', -1) is not None)
check('금액 bool 거부', v('acc-002', 'acc-003', True) is not None)
check('금액 소수 거부', v('acc-002', 'acc-003', 1000.5) is not None)
check('같은 계좌 거부', v('acc-002', 'acc-002', 1000) is not None)
check('남의 계좌(출금) 거부', v('acc-004', 'acc-001', 1000) is not None)
check('남의 계좌(입금) 거부', v('acc-001', 'acc-004', 1000) is not None)
check('없는 계좌 거부', v('acc-001', 'acc-999', 1000) is not None)
check('남의 계좌/없는 계좌 메시지 동일', v('acc-001', 'acc-004', 1000) == v('acc-001', 'acc-999', 1000))
check('잔액 부족 거부', v('acc-003', 'acc-002', 500000) is not None, v('acc-003', 'acc-002', 500000))
check('잔액과 같은 금액은 허용', v('acc-003', 'acc-002', 390000) is None)

# ---------- 2. 변경 함수 ----------
fresh()
before = data_store.load_data()
new_data, record = functions.apply_transfer(before, OWNER, 'acc-002', 'acc-003', 500000, functions.get_transfer_time())
check('원본 dict는 바뀌지 않음', [a['balance'] for a in before['accounts']] == [1431800, 2280000, 390000, 850000, 1200000])
bal_new = {a['account_id']: a['balance'] for a in new_data['accounts']}
check('출금/입금 잔액 반영', bal_new['acc-002'] == 1780000 and bal_new['acc-003'] == 890000)
check('전체 잔액 합계 보존', sum(bal_new.values()) == 1431800 + 2280000 + 390000 + 850000 + 1200000)
new_tx = new_data['transactions'][-2:]
check('거래 2건 생성(tx-021 출금, tx-022 입금)',
      [(t['transaction_id'], t['type'], t['account_id'], t['amount']) for t in new_tx]
      == [('tx-021', 'withdrawal', 'acc-002', 500000), ('tx-022', 'deposit', 'acc-003', 500000)])
check('거래에 카드 없음, 처리 번호 연결', all(t['card_id'] is None and t['request_id'] == 'req-001' for t in new_tx))
check('처리 기록 생성', record['status'] == 'completed' and new_data['requests'][-1] == record)

# ---------- 3. 저장(원자성) ----------
fresh()
original = raw()
real_dump = json.dump
try:
    json.dump = lambda *a, **k: (_ for _ in ()).throw(OSError('disk full'))
    try:
        data_store.save_data(new_data)
        raised = False
    except OSError:
        raised = True
finally:
    json.dump = real_dump
check('저장 실패 시 예외 전파', raised)
check('저장 실패 시 기존 파일 그대로', raw() == original)
check('저장 실패 시 임시 파일 남지 않음', not list(TMP_DIR.glob('*.tmp')))
data_store.save_data(new_data)
check('저장 성공 시 반영', bal('acc-002') == 1780000)


# ---------- 4. 그래프 (LLM 없이: 추출 노드만 가짜) ----------
def make_app(from_account='acc-002', to_account='acc-003', amount=500000, classifier=None):
    def fake_extract(state):
        return {'transfer': TransferAccounts(from_account=from_account, to_account=to_account, amount=amount)}

    builder = StateGraph(BankState)
    builder.add_node('transfer_agent', build_transfer_graph(fake_extract, classifier or graph.classify_response))
    builder.add_edge(START, 'transfer_agent')
    builder.add_edge('transfer_agent', END)
    return builder.compile(checkpointer=InMemorySaver(serde=graph.checkpoint_serde))


def start(app, tid):
    cfg = {'configurable': {'thread_id': tid}}
    r = app.invoke({'messages': [HumanMessage(content='이체해줘')], 'owner_id': OWNER}, config=cfg)
    return cfg, r


# 4-1 승인
fresh()
app = make_app()
cfg, r = start(app, 'approve')
payload = r['__interrupt__'][0].value
check('승인 전 interrupt로 멈춤', '__interrupt__' in r)
check('승인 화면에 별명·금액·전후 잔액 포함',
      all(s in payload['message'] for s in ['저축', '여행 자금', '500,000원', '2,280,000원', '1,780,000원', '390,000원', '890,000원']))
check('승인 대기 중에는 데이터 변경 없음', bal('acc-002') == 2280000 and bal('acc-003') == 390000)
r = app.invoke(Command(resume='승인'), config=cfg)
check('승인 후 잔액 반영', bal('acc-002') == 1780000 and bal('acc-003') == 890000)
data = data_store.load_data()
check('승인 후 거래 2건·처리 기록 저장', len(data['transactions']) == 22 and len(data['requests']) == 1)
check('완료 메시지', '이체가 완료되었습니다' in r['messages'][-1].content and 'req-001' in r['messages'][-1].content)
check('종료 후 State.transfer 비워짐', app.get_state(cfg).values['transfer'] is None)

# 4-2 거절
fresh()
original = raw()
app = make_app()
cfg, r = start(app, 'reject')
r = app.invoke(Command(resume='거절'), config=cfg)
check('거절 시 파일이 한 바이트도 바뀌지 않음', raw() == original)
check('거절 메시지', '취소' in r['messages'][-1].content)
check('거절 후 State.transfer 비워짐', app.get_state(cfg).values['transfer'] is None)

# 4-3 검증 실패는 승인 요청 없이 종료
for name, kw in [('남의 계좌로 입금', dict(to_account='acc-004')),
                 ('남의 계좌에서 출금', dict(from_account='acc-004', to_account='acc-001')),
                 ('잔액 부족', dict(from_account='acc-003', to_account='acc-002', amount=500000)),
                 ('같은 계좌', dict(to_account='acc-002'))]:
    fresh()
    original = raw()
    app = make_app(**kw)
    cfg, r = start(app, name)
    check(f'검증 실패({name}): 승인 요청 없이 종료', '__interrupt__' not in r and '진행할 수 없습니다' in r['messages'][-1].content)
    check(f'검증 실패({name}): 데이터 변경 없음', raw() == original)
    check(f'검증 실패({name}): State.transfer 비워짐', app.get_state(cfg).values['transfer'] is None)

# 4-4 승인 대기 중 잔액이 바뀌면 실행 직전 재검증에서 막힘
fresh()
app = make_app()
cfg, r = start(app, 'revalidate')
drained = data_store.load_data()
next(a for a in drained['accounts'] if a['account_id'] == 'acc-002')['balance'] = 100000
data_store.save_data(drained)
snapshot = raw()
r = app.invoke(Command(resume='승인'), config=cfg)
check('재검증 실패 메시지', '승인 이후' in r['messages'][-1].content)
check('재검증 실패 시 데이터 변경 없음', raw() == snapshot)

# 4-5 저장 실패 시 이체가 반영되지 않음
fresh()
original = raw()
app = make_app()
cfg, r = start(app, 'savefail')
real_save = graph.save_data
graph.save_data = lambda data: (_ for _ in ()).throw(OSError('disk full'))
try:
    r = app.invoke(Command(resume='승인'), config=cfg)
finally:
    graph.save_data = real_save
check('저장 실패 메시지', '반영되지 않았습니다' in r['messages'][-1].content)
check('저장 실패 시 데이터 변경 없음', raw() == original)

# 4-6 이체 후 거래내역 조회 tool에 반영
fresh()
app = make_app()
cfg, r = start(app, 'view')
app.invoke(Command(resume='승인'), config=cfg)
res = json.loads(functions.get_account_transactions_by_owner.invoke({'owner_id': OWNER, 'period': 'today'}))
check('이체 거래가 오늘 거래내역에 나타남', res['count'] == 2 + 2, f'count={res["count"]}')
check('오늘 출금·입금 합계에 이체 반영', res['total_deposit'] >= 500000 and res['total_withdrawal'] >= 500000)

# ---------- 5. 승인 흐름: 자연어 응답 해석, 승인 전 수정 (LLM 분류기만 가짜) ----------
R = graph.ResponseInterpretation
RESET_FIELDS = ('transfer', 'transfer_response', 'transfer_decision', 'transfer_notice')


def scripted(*items):
    """미리 정한 분류 결과를 순서대로 돌려주는 가짜 분류기. Exception이면 raise, None이면 None을 반환한다."""
    queue = list(items)
    calls = []

    def classifier(state):
        calls.append(state['transfer_response'])
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    classifier.calls = calls
    return classifier


def reply(app, cfg, text):
    return app.invoke(Command(resume=text), config=cfg)


def screen(r):
    return r['__interrupt__'][0].value['message']


def values(app, cfg):
    """멈춰 있는 동안에는 서브그래프 안의 State를, 끝난 뒤에는 부모의 State를 돌려준다"""
    snapshot = app.get_state(cfg, subgraphs=True)
    for task in snapshot.tasks:
        if task.state is not None and hasattr(task.state, 'values'):
            return task.state.values
    return snapshot.values


def all_reset(app, cfg):
    return all(values(app, cfg).get(k) is None for k in RESET_FIELDS)


# 5-1 정확히 "승인"/"거절"은 LLM을 거치지 않고, 그 밖의 말은 LLM을 거침
fresh()
clf = scripted()
app = make_app(classifier=clf)
cfg, r = start(app, 'fast')
r = reply(app, cfg, '  승인 ')
check('정확한 "승인"은 공백이 있어도 LLM 없이 승인', clf.calls == [] and bal('acc-002') == 1780000)
fresh()
clf = scripted()
app = make_app(classifier=clf)
cfg, r = start(app, 'fastreject')
r = reply(app, cfg, '거절')
check('정확한 "거절"은 LLM 없이 취소', clf.calls == [] and '취소' in r['messages'][-1].content and bal('acc-002') == 2280000)
fresh()
clf = scripted(R(action='approve'))
app = make_app(classifier=clf)
cfg, r = start(app, 'notexact')
r = reply(app, cfg, '승인합니다')
check('"승인합니다"처럼 정확히 같지 않으면 LLM이 해석', clf.calls == ['승인합니다'] and bal('acc-002') == 1780000)

# 5-2 자연어 승인
fresh()
app = make_app(classifier=scripted(R(action='approve')))
cfg, r = start(app, 'nl-approve')
r = reply(app, cfg, '응, 진행해')
check('자연어 승인으로 이체 실행', bal('acc-002') == 1780000 and '이체가 완료되었습니다' in r['messages'][-1].content)
check('자연어 승인 후 모든 이체 State 비워짐', all_reset(app, cfg))

# 5-3 자연어 취소
fresh()
original = raw()
app = make_app(classifier=scripted(R(action='reject')))
cfg, r = start(app, 'nl-reject')
r = reply(app, cfg, '취소할게')
check('자연어 취소는 변경 없이 종료', raw() == original and '취소' in r['messages'][-1].content)
check('자연어 취소 후 모든 이체 State 비워짐', all_reset(app, cfg))

# 5-4 금액 수정 → 전체 화면 다시 → 새 승인 → 수정된 금액으로 실행
fresh()
original = raw()
app = make_app(classifier=scripted(R(action='edit', amount=50000)))
cfg, r = start(app, 'edit-amount')
r = reply(app, cfg, '아니, 5만 원만')
check('수정하면 다시 승인 화면(interrupt)', '__interrupt__' in r)
check('수정한 금액이 전체 화면에 반영', '50,000원' in screen(r) and '2,230,000원' in screen(r) and '440,000원' in screen(r))
check('수정 안내가 화면 맨 위에 표시', screen(r).startswith('수정한 내용을 반영했습니다'))
check('수정만으로는 데이터가 바뀌지 않음', raw() == original)
check('State.transfer의 금액이 갱신됨', values(app, cfg)['transfer'].amount == 50000)
r = reply(app, cfg, '승인')
d = data_store.load_data()
check('새로 승인하면 수정된 금액으로 실행', bal('acc-002') == 2230000 and bal('acc-003') == 440000 and d['transactions'][-1]['amount'] == 50000)
check('수정 뒤 완료 후 모든 이체 State 비워짐', all_reset(app, cfg))

# 5-5 계좌 수정
fresh()
app = make_app(classifier=scripted(R(action='edit', to_account='acc-001')))
cfg, r = start(app, 'edit-account')
r = reply(app, cfg, '여행 자금 말고 생활비로')
check('입금 계좌 수정이 화면에 반영', '생활비(acc-001)' in screen(r) and '여행 자금' not in screen(r))
r = reply(app, cfg, '승인')
check('수정된 계좌로 실행(저축→생활비)', bal('acc-002') == 1780000 and bal('acc-001') == 1931800 and bal('acc-003') == 390000)

# 5-6 수정 후 검증 실패: 이전 안 유지 + 이유 안내 + 다시 승인 화면
for name, edit, expect in [
    ('잔액 초과 금액', R(action='edit', amount=99999999), '잔액'),
    ('남의 계좌로 수정', R(action='edit', to_account='acc-004'), '본인 소유'),
    ('출금·입금이 같아짐', R(action='edit', to_account='acc-002'), '같습니다'),
    ('0원 이하 금액', R(action='edit', amount=-5), '0보다 큰'),
]:
    fresh()
    original = raw()
    app = make_app(classifier=scripted(edit))
    cfg, r = start(app, f'badedit-{name}')
    r = reply(app, cfg, '수정해줘')
    check(f'수정 실패({name}): 다시 승인 화면', '__interrupt__' in r)
    check(f'수정 실패({name}): 이유 안내', '반영할 수 없습니다' in screen(r) and expect in screen(r), screen(r).splitlines()[0])
    check(f'수정 실패({name}): 이전 안(500,000원 저축→여행 자금) 유지',
          '500,000원' in screen(r) and values(app, cfg)['transfer'].amount == 500000 and values(app, cfg)['transfer'].to_account == 'acc-003')
    check(f'수정 실패({name}): 데이터 변경 없음', raw() == original)
    r = reply(app, cfg, '승인')
    check(f'수정 실패({name}) 뒤 승인하면 원래 안으로 실행', bal('acc-002') == 1780000 and bal('acc-003') == 890000)

# 5-7 규칙: 승인과 수정이 섞이면 수정만 반영하고 새로 승인받는다
fresh()
original = raw()
app = make_app(classifier=scripted(R(action='approve', amount=30000)))
cfg, r = start(app, 'mixed')
r = reply(app, cfg, '3만 원으로 하고 진행해')
check('승인+수정 혼합: 실행하지 않고 다시 승인 화면', '__interrupt__' in r and raw() == original)
check('승인+수정 혼합: 수정 금액이 화면에 반영', '30,000원' in screen(r))
check('승인+수정 혼합: 분류 결과가 수정으로 기록', values(app, cfg)['transfer'].amount == 30000)
r = reply(app, cfg, '승인')
check('승인+수정 혼합: 새로 승인해야 실행', bal('acc-002') == 2250000 and bal('acc-003') == 420000)

# 5-8 규칙: 애매하거나 분류가 실패하면 절대 승인하지 않는다
for name, item in [('unclear', R(action='unclear')), ('LLM 오류', RuntimeError('LLM down')), ('None 반환', None)]:
    fresh()
    original = raw()
    app = make_app(classifier=scripted(item))
    cfg, r = start(app, f'unsure-{name}')
    r = reply(app, cfg, '음... 글쎄')
    check(f'{name}: 승인하지 않고 다시 묻기', '__interrupt__' in r and raw() == original)
    check(f'{name}: 이유 안내', '이해하지 못했습니다' in screen(r))
    check(f'{name}: 이전 안 유지', values(app, cfg)['transfer'].amount == 500000)

# 5-9 수정인데 바뀐 값이 없거나 현재와 같은 경우
for name, item, expect in [('값 없음', R(action='edit'), '파악하지 못했습니다'),
                           ('현재와 동일', R(action='edit', amount=500000), '같습니다')]:
    fresh()
    original = raw()
    app = make_app(classifier=scripted(item))
    cfg, r = start(app, f'noop-{name}')
    r = reply(app, cfg, '아니')
    check(f'수정({name}): 다시 묻고 안내', '__interrupt__' in r and expect in screen(r) and raw() == original)

# 5-10 여러 번 수정한 뒤 승인 / 수정 도중 거절
fresh()
app = make_app(classifier=scripted(R(action='edit', amount=300000), R(action='edit', to_account='acc-001')))
cfg, r = start(app, 'multi')
r = reply(app, cfg, '30만 원으로')
r = reply(app, cfg, '생활비로')
check('수정을 두 번 거치면 화면에 둘 다 반영', '300,000원' in screen(r) and '생활비(acc-001)' in screen(r))
r = reply(app, cfg, '승인')
check('마지막 이체안으로 한 번만 실행', bal('acc-002') == 1980000 and bal('acc-001') == 1731800 and len(data_store.load_data()['requests']) == 1)

fresh()
original = raw()
app = make_app(classifier=scripted(R(action='edit', amount=100000)))
cfg, r = start(app, 'edit-then-reject')
r = reply(app, cfg, '10만 원으로')
r = reply(app, cfg, '거절')
check('수정한 뒤 거절하면 변경 없이 종료', raw() == original and '취소' in r['messages'][-1].content and all_reset(app, cfg))

# 5-11 실행 노드 방어: 승인 확인 없이는 실행하지 않음
fresh()
original = raw()
for name, decision in [('결정 없음', None), ('수정', 'edit'), ('불명확', 'unclear')]:
    out = graph.transfer_execute({'messages': [], 'owner_id': OWNER,
                                  'transfer': TransferAccounts(from_account='acc-002', to_account='acc-003', amount=500000),
                                  'transfer_decision': decision})
    check(f'실행 방어({name}): 실행 거부', '승인이 확인되지 않아' in out['messages'][0].content and out['transfer'] is None and raw() == original)

shutil.rmtree(TMP_DIR, ignore_errors=True)
print('\n모든 이체 테스트 통과')
