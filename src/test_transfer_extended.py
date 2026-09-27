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
from graph import BankState, TransferAccounts, TransferLeg, SplitTransfer, build_transfer_graph

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
# fresh() 기준 acc-002 잔액 2,280,000원

# ---------- 1. 검증/변경 함수 (functions.py) ----------
fresh()
d = data_store.load_data()
check('keep_balance 계산: 2,280,000 - 300,000 = 1,980,000',
      functions.compute_keep_balance_amount(d, OWNER, 'acc-002', 300000) == 1980000)
check('keep_balance 계산: 남의 계좌면 None',
      functions.compute_keep_balance_amount(d, OWNER, 'acc-004', 0) is None)

vlegs = lambda legs: functions.validate_transfer_legs(d, OWNER, 'acc-002', legs)
check('분할 검증: 정상 통과', vlegs([('acc-001', 100000), ('acc-003', 200000)]) is None)
check('분할 검증: 총액이 잔액 초과면 거부', vlegs([('acc-001', 2000000), ('acc-003', 2000000)]) is not None)
check('분할 검증: 남의 계좌 포함되면 거부', vlegs([('acc-001', 100000), ('acc-004', 100000)]) is not None)
check('분할 검증: 금액 0 이하 거부', vlegs([('acc-001', 0)]) is not None)
check('분할 검증: 출금=입금 거부', vlegs([('acc-002', 1000)]) is not None)

new_data, records = functions.apply_transfer_legs(
    d, OWNER, 'acc-002', [('acc-001', 100000), ('acc-003', 200000)], functions.get_transfer_time()
)
check('원본 dict는 바뀌지 않음', bal('acc-002') == 2280000)
bal_new = {a['account_id']: a['balance'] for a in new_data['accounts']}
check('분할 이체: 출금 계좌에서 총액 차감', bal_new['acc-002'] == 2280000 - 300000)
check('분할 이체: 각 입금 계좌 반영', bal_new['acc-001'] == 1531800 and bal_new['acc-003'] == 590000)
check('분할 이체: 처리 기록 2건(leg마다 1건)', len(records) == 2 and len(new_data['requests']) == 2)
check('분할 이체: 거래 4건 생성(leg마다 출금·입금)', len(new_data['transactions']) == len(d['transactions']) + 4)

# ---------- 2. 그래프: 조건부(잔액 유지) 이체 ----------
def make_keep_app(from_account='acc-002', to_account='acc-003', keep_amount=300000, classifier=None):
    def fake_extract(state):
        data = data_store.load_data()
        amount = functions.compute_keep_balance_amount(data, OWNER, from_account, keep_amount)
        if amount is None or amount <= 0:
            return graph.transfer_end(f'남길 금액({keep_amount:,}원)을 제외하면 이체할 금액이 없어 이체를 진행하지 않았습니다.')
        return {'transfer': TransferAccounts(from_account=from_account, to_account=to_account,
                                              amount=amount, keep_amount=keep_amount)}

    builder = StateGraph(BankState)
    builder.add_node('transfer_agent', build_transfer_graph(fake_extract, classifier or graph.classify_response))
    builder.add_edge(START, 'transfer_agent')
    builder.add_edge('transfer_agent', END)
    return builder.compile(checkpointer=InMemorySaver(serde=graph.checkpoint_serde))


def start(app, tid, text='이체해줘'):
    cfg = {'configurable': {'thread_id': tid}}
    r = app.invoke({'messages': [HumanMessage(content=text)], 'owner_id': OWNER}, config=cfg)
    return cfg, r


def screen(r):
    return r['__interrupt__'][0].value['message']


# 2-1 정상 승인: 2,280,000 - 300,000 = 1,980,000원 이체
fresh()
app = make_keep_app()
cfg, r = start(app, 'keep-approve')
check('조건부 이체 승인 화면에 남길 금액·계산된 금액 표시',
      all(s in screen(r) for s in ['남길 금액: 300,000원', '1,980,000원']))
r = app.invoke(Command(resume='승인'), config=cfg)
check('조건부 이체 실행: 계산된 금액만큼 이체', bal('acc-002') == 300000 and bal('acc-003') == 390000 + 1980000)

# 2-2 남길 금액 이상이면(이체액 <=0) 실행하지 않음
fresh()
app = make_keep_app(keep_amount=5000000)  # 잔액(2,280,000)보다 큰 keep_amount
cfg, r = start(app, 'keep-noamount')
check('남길 금액이 잔액보다 크면 승인 없이 종료', '__interrupt__' not in r and '이체할 금액이 없어' in r['messages'][-1].content)
check('데이터 변경 없음', bal('acc-002') == 2280000)

# 2-3 승인 대기 중 잔액이 바뀌면(줄어듦) 실행 직전 다시 계산해 재승인 요청
fresh()
app = make_keep_app()  # keep 300,000 -> 예상 이체액 1,980,000원
cfg, r = start(app, 'keep-revalidate')
drained = data_store.load_data()
next(a for a in drained['accounts'] if a['account_id'] == 'acc-002')['balance'] = 1000000
data_store.save_data(drained)
r = app.invoke(Command(resume='승인'), config=cfg)
check('잔액이 바뀌면 실행하지 않고 새 금액으로 다시 승인 화면', '__interrupt__' in r)
check('다시 계산된 금액(1,000,000 - 300,000 = 700,000원) 표시', '700,000원' in screen(r))
check('재계산 안내 문구', '다시 계산했습니다' in screen(r))
check('아직 실행되지 않음(acc-003 그대로)', bal('acc-003') == 390000)
r = app.invoke(Command(resume='승인'), config=cfg)
check('다시 승인하면 새 금액으로 실행', bal('acc-002') == 300000 and bal('acc-003') == 390000 + 700000)

# 2-4 조건부 이체는 자연어 수정("edit")을 지원하지 않는다
fresh()
R = graph.ResponseInterpretation
app = make_keep_app(classifier=lambda state: R(action='edit', amount=1))
cfg, r = start(app, 'keep-no-edit')
r = app.invoke(Command(resume='5만원으로 바꿔줘'), config=cfg)
check('조건부 이체 수정 시도는 unclear로 처리되어 다시 승인 화면', '__interrupt__' in r and '수정할 수 없습니다' in screen(r))
check('데이터 변경 없음', bal('acc-002') == 2280000)

# ---------- 3. 그래프: 분할 이체 ----------
def make_split_app(from_account='acc-002', legs=(('acc-001', 100000), ('acc-003', 200000)), classifier=None):
    def fake_extract(state):
        return {'transfer': SplitTransfer(
            from_account=from_account,
            legs=[TransferLeg(to_account=t, amount=a) for t, a in legs],
        )}

    builder = StateGraph(BankState)
    builder.add_node('transfer_agent', build_transfer_graph(fake_extract, classifier or graph.classify_response))
    builder.add_edge(START, 'transfer_agent')
    builder.add_edge('transfer_agent', END)
    return builder.compile(checkpointer=InMemorySaver(serde=graph.checkpoint_serde))


# 3-1 정상 승인
fresh()
app = make_split_app()
cfg, r = start(app, 'split-approve')
check('분할 이체 승인 화면에 계좌별 금액과 총액 표시',
      all(s in screen(r) for s in ['100,000원', '200,000원', '총 이체 금액: 300,000원']))
check('승인 대기 중에는 데이터 변경 없음', bal('acc-002') == 2280000)
r = app.invoke(Command(resume='승인'), config=cfg)
check('분할 이체 실행: 각 계좌에 반영', bal('acc-002') == 1980000 and bal('acc-001') == 1531800 and bal('acc-003') == 590000)
data = data_store.load_data()
check('분할 이체: 처리 기록 2건, 거래 4건', len(data['requests']) == 2 and len(data['transactions']) == 24)
check('완료 메시지', '분할 이체가 완료되었습니다' in r['messages'][-1].content)

# 3-2 거절
fresh()
original = raw()
app = make_split_app()
cfg, r = start(app, 'split-reject')
r = app.invoke(Command(resume='거절'), config=cfg)
check('분할 이체 거절: 변경 없음', raw() == original and '취소' in r['messages'][-1].content)

# 3-3 검증 실패(총액이 잔액 초과)는 승인 요청 없이 종료
fresh()
original = raw()
app = make_split_app(legs=(('acc-001', 2000000), ('acc-003', 2000000)))
cfg, r = start(app, 'split-insufficient')
check('총액 부족 검증 실패: 승인 없이 종료', '__interrupt__' not in r and '진행할 수 없습니다' in r['messages'][-1].content)
check('데이터 변경 없음', raw() == original)

# 3-4 승인 대기 중 잔액이 줄어들면 실행 직전 재검증에서 막히고, legs는 전혀 반영되지 않는다(원자성)
fresh()
app = make_split_app()
cfg, r = start(app, 'split-atomic')
drained = data_store.load_data()
next(a for a in drained['accounts'] if a['account_id'] == 'acc-002')['balance'] = 150000
data_store.save_data(drained)
snapshot = raw()
r = app.invoke(Command(resume='승인'), config=cfg)
check('재검증 실패 메시지', '승인 이후' in r['messages'][-1].content)
check('재검증 실패 시 legs 중 일부도 반영되지 않음(원자성)', raw() == snapshot)

# 3-5 분할 이체는 자연어 수정을 지원하지 않는다
fresh()
app = make_split_app(classifier=lambda state: graph.ResponseInterpretation(action='edit', amount=1))
cfg, r = start(app, 'split-no-edit')
r = app.invoke(Command(resume='금액 바꿔줘'), config=cfg)
check('분할 이체 수정 시도는 unclear로 처리되어 다시 승인 화면', '__interrupt__' in r and '수정할 수 없습니다' in screen(r))
check('데이터 변경 없음', bal('acc-002') == 2280000)

shutil.rmtree(TMP_DIR, ignore_errors=True)
print('\n모든 조건부·분할 이체 테스트 통과')
