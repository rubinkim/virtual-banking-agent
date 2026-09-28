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
from graph import (
    BankState, CardStatusAction, ReissueAction,
    build_card_status_graph, build_reissue_graph, build_card_lost_and_reissue_graph,
)

TMP_DIR = Path(tempfile.mkdtemp())
data_store.DATA_PATH = TMP_DIR / 'data.json'


def fresh():
    shutil.copyfile(data_store.INITIAL_PATH, data_store.DATA_PATH)


def raw():
    return data_store.DATA_PATH.read_bytes()


def status_of(card_id):
    return next(c['status'] for c in data_store.load_data()['cards'] if c['card_id'] == card_id)


def check(name, cond, detail=''):
    assert cond, f'FAIL - {name} {detail}'
    print('PASS -', name, detail)


OWNER = 'user-001'
# fresh() 기준: card-001 생활비 카드(acc-001, active), card-002 여행 카드(acc-003, active)
# addr-home(집), addr-work(회사) 둘 다 user-001 소유

R_STATUS = graph.CardStatusResponseInterpretation
R_REISSUE = graph.ReissueResponseInterpretation

# ---------- 1. 카드 상태 변경 검증/변경 함수 ----------
fresh()
d = data_store.load_data()
v = lambda card, kind: functions.validate_card_status_change(d, OWNER, card, kind)
check('일시 잠금 정상 통과', v('card-001', 'lock') is None)
check('분실 정지 정상 통과', v('card-001', 'lost') is None)
check('잠금 해제는 active 카드에는 거부', v('card-001', 'unlock') is not None)
check('남의 카드 거부', v('card-003', 'lock') is not None)
check('없는 카드 거부', v('card-999', 'lock') is not None)

new_data, record = functions.apply_card_status_change(d, OWNER, 'card-001', 'lock')
check('원본 dict는 바뀌지 않음', status_of('card-001') == 'active')
check('상태가 locked로 바뀜', next(c['status'] for c in new_data['cards'] if c['card_id'] == 'card-001') == 'locked')
check('처리 기록 생성', record['type'] == 'card_status_change' and record['old_status'] == 'active' and record['new_status'] == 'locked')

# lock된 카드 기준 재검증
v2 = lambda card, kind: functions.validate_card_status_change(new_data, OWNER, card, kind)
check('locked 카드는 다시 lock 불가', v2('card-001', 'lock') is not None)
check('locked 카드는 unlock 가능', v2('card-001', 'unlock') is None)
check('locked 카드도 분실 정지 가능', v2('card-001', 'lost') is None)

lost_data, _ = functions.apply_card_status_change(new_data, OWNER, 'card-001', 'lost')
v3 = lambda card, kind: functions.validate_card_status_change(lost_data, OWNER, card, kind)
check('lost 카드는 다시 lost 불가', v3('card-001', 'lost') is not None)
check('lost 카드는 lock 불가', v3('card-001', 'lock') is not None)
check('lost 카드는 unlock 불가', v3('card-001', 'unlock') is not None)

# ---------- 2. 재발급 신청 검증/변경 함수 ----------
fresh()
d = data_store.load_data()
check('active 카드는 재발급 신청 불가', functions.validate_reissue_create(d, OWNER, 'card-001', 'addr-home') is not None)

lost_data, _ = functions.apply_card_status_change(d, OWNER, 'card-001', 'lost')
check('lost 카드는 재발급 신청 가능', functions.validate_reissue_create(lost_data, OWNER, 'card-001', 'addr-home') is None)
check('남의 카드 거부', functions.validate_reissue_create(lost_data, OWNER, 'card-003', 'addr-home') is not None)
check('등록되지 않은 배송지 거부', functions.validate_reissue_create(lost_data, OWNER, 'card-001', 'addr-none') is not None)

new_data, record = functions.apply_reissue_create(lost_data, OWNER, 'card-001', 'addr-home')
check('신청이 추가됨', len(new_data['reissue_applications']) == 1 and record['status'] == 'received')
check('원본에는 반영 안 됨', len(lost_data['reissue_applications']) == 0)

check('취소되지 않은 신청이 있으면 새 신청 거부', functions.validate_reissue_create(new_data, OWNER, 'card-001', 'addr-work') is not None)
check('접수 상태에서는 배송지 변경 가능', functions.validate_reissue_edit(new_data, OWNER, record['reissue_id'], 'addr-work') is None)
check('같은 배송지로 변경은 거부', functions.validate_reissue_edit(new_data, OWNER, record['reissue_id'], 'addr-home') is not None)
check('접수 상태에서는 취소 가능', functions.validate_reissue_cancel(new_data, OWNER, record['reissue_id']) is None)

edited_data, edited = functions.apply_reissue_edit(new_data, OWNER, record['reissue_id'], 'addr-work')
check('배송지가 바뀜', edited['delivery_address_id'] == 'addr-work')

# 제작 중 상태는 테스트 데이터로 직접 주입해 수정·취소 제한을 확인한다
in_production = json.loads(json.dumps(new_data))
in_production['reissue_applications'][0]['status'] = 'in_production'
check('제작 중이면 배송지 변경 거부', functions.validate_reissue_edit(in_production, OWNER, record['reissue_id'], 'addr-work') is not None)
check('제작 중이면 취소 거부', functions.validate_reissue_cancel(in_production, OWNER, record['reissue_id']) is not None)

shipping = json.loads(json.dumps(new_data))
shipping['reissue_applications'][0]['status'] = 'shipping'
check('배송 중이면 배송지 변경 거부', functions.validate_reissue_edit(shipping, OWNER, record['reissue_id'], 'addr-work') is not None)
check('배송 중이면 취소 거부', functions.validate_reissue_cancel(shipping, OWNER, record['reissue_id']) is not None)

cancelled_data, cancelled = functions.apply_reissue_cancel(new_data, OWNER, record['reissue_id'])
check('취소 상태로 바뀜', cancelled['status'] == 'cancelled')
check('취소된 신청이 있어도 새로 신청 가능', functions.validate_reissue_create(cancelled_data, OWNER, 'card-001', 'addr-home') is None)


# ---------- 3. card_status_agent 그래프 ----------
def make_status_app(card_id='card-001', kind='lock', classifier=None):
    def fake_extract(state):
        return {'card_status_action': CardStatusAction(card_id=card_id, kind=kind)}

    builder = StateGraph(BankState)
    builder.add_node('card_status_agent', build_card_status_graph(fake_extract, classifier or graph.classify_card_status_response))
    builder.add_edge(START, 'card_status_agent')
    builder.add_edge('card_status_agent', END)
    return builder.compile(checkpointer=InMemorySaver(serde=graph.checkpoint_serde))


def start(app, tid, text='카드 처리해줘'):
    cfg = {'configurable': {'thread_id': tid}}
    r = app.invoke({'messages': [HumanMessage(content=text)], 'owner_id': OWNER}, config=cfg)
    return cfg, r


def screen(r):
    return r['__interrupt__'][0].value['message']


fresh()
app = make_status_app(kind='lock')
cfg, r = start(app, 'lock-approve')
check('승인 화면에 카드·상태 전후 표시', '생활비 카드(card-001)' in screen(r) and '사용 가능' in screen(r) and '일시 잠금' in screen(r))
check('승인 대기 중에는 데이터 변경 없음', status_of('card-001') == 'active')
r = app.invoke(Command(resume='승인'), config=cfg)
check('승인 후 상태 반영', status_of('card-001') == 'locked')
check('완료 메시지', '일시 잠금' in r['messages'][-1].content)

fresh()
original = raw()
app = make_status_app(kind='lost')
cfg, r = start(app, 'lost-reject')
r = app.invoke(Command(resume='거절'), config=cfg)
check('거절 시 변경 없음', raw() == original and '취소' in r['messages'][-1].content)

fresh()
original = raw()
app = make_status_app(kind='unlock')  # active 카드는 잠금 해제 불가
cfg, r = start(app, 'unlock-invalid')
check('검증 실패: 승인 요청 없이 종료', '__interrupt__' not in r and '처리할 수 없습니다' in r['messages'][-1].content)
check('검증 실패: 데이터 변경 없음', raw() == original)

fresh()
app = make_status_app(kind='lost')
cfg, r = start(app, 'unclear')
app2 = app
r = app.invoke(Command(resume='음...'), config=cfg)
check('애매한 응답은 다시 승인 화면', '__interrupt__' in r and '이해하지 못했습니다' in screen(r))
r = app.invoke(Command(resume='승인'), config=cfg)
check('다시 승인하면 실행', status_of('card-001') == 'lost')

fresh()
app = make_status_app(kind='lock', classifier=lambda state: R_STATUS(action='reject'))
cfg, r = start(app, 'nl-reject')
r = app.invoke(Command(resume='아니 됐어'), config=cfg)
check('자연어 거절은 변경 없이 종료', status_of('card-001') == 'active' and '취소' in r['messages'][-1].content)

fresh()
app = make_status_app(kind='lost')
cfg, r = start(app, 'revalidate')
drained = data_store.load_data()
next(c for c in drained['cards'] if c['card_id'] == 'card-001')['status'] = 'lost'
data_store.save_data(drained)
r = app.invoke(Command(resume='승인'), config=cfg)
check('재검증 실패 메시지', '승인 이후' in r['messages'][-1].content)


# ---------- 4. reissue_agent 그래프 ----------
def make_reissue_app(action: ReissueAction, classifier=None):
    def fake_extract(state):
        return {'reissue_action': action}

    builder = StateGraph(BankState)
    builder.add_node('reissue_agent', build_reissue_graph(fake_extract, classifier or graph.classify_reissue_response))
    builder.add_edge(START, 'reissue_agent')
    builder.add_edge('reissue_agent', END)
    return builder.compile(checkpointer=InMemorySaver(serde=graph.checkpoint_serde))


def seed_lost_and_reissue(address='addr-home'):
    """card-001을 분실 정지시키고, 재발급 신청 1건을 만든 뒤 raw 데이터를 반환한다"""
    data = data_store.load_data()
    lost_data, _ = functions.apply_card_status_change(data, OWNER, 'card-001', 'lost')
    data_store.save_data(lost_data)
    new_data, record = functions.apply_reissue_create(lost_data, OWNER, 'card-001', address)
    data_store.save_data(new_data)
    return record['reissue_id']


fresh()
lost_data, _ = functions.apply_card_status_change(data_store.load_data(), OWNER, 'card-001', 'lost')
data_store.save_data(lost_data)
app = make_reissue_app(ReissueAction(kind='create', card_id='card-001', delivery_address_id='addr-home'))
cfg, r = start(app, 'reissue-approve')
check('신청 승인 화면에 카드·배송지 표시', '생활비 카드' in screen(r) and '집' in screen(r))
r = app.invoke(Command(resume='승인'), config=cfg)
data = data_store.load_data()
check('신청이 저장됨', len(data['reissue_applications']) == 1 and data['reissue_applications'][0]['status'] == 'received')
check('완료 메시지에 신청 번호 포함', '신청 번호' in r['messages'][-1].content)

fresh()
rid = seed_lost_and_reissue()
app = make_reissue_app(ReissueAction(kind='create', card_id='card-001', delivery_address_id='addr-work'))
cfg, r = start(app, 'reissue-duplicate')
check('중복 신청 검증 실패: 승인 요청 없이 종료', '__interrupt__' not in r and '이미 취소되지 않은' in r['messages'][-1].content)

fresh()
rid = seed_lost_and_reissue()
app = make_reissue_app(ReissueAction(kind='edit', reissue_id=rid, delivery_address_id='addr-work'))
cfg, r = start(app, 'reissue-edit')
check('배송지 변경 승인 화면', '집' in screen(r) and '회사' in screen(r))
r = app.invoke(Command(resume='승인'), config=cfg)
data = data_store.load_data()
check('배송지가 바뀜', data['reissue_applications'][0]['delivery_address_id'] == 'addr-work')

fresh()
rid = seed_lost_and_reissue()
app = make_reissue_app(ReissueAction(kind='cancel', reissue_id=rid))
cfg, r = start(app, 'reissue-cancel')
r = app.invoke(Command(resume='승인'), config=cfg)
data = data_store.load_data()
check('취소됨', data['reissue_applications'][0]['status'] == 'cancelled')

fresh()
rid = seed_lost_and_reissue()
drained = data_store.load_data()
drained['reissue_applications'][0]['status'] = 'in_production'
data_store.save_data(drained)
app = make_reissue_app(ReissueAction(kind='cancel', reissue_id=rid))
cfg, r = start(app, 'reissue-cancel-blocked')
check('제작 중이면 취소 검증 실패로 종료', '__interrupt__' not in r and '취소할 수 없습니다' in r['messages'][-1].content)

fresh()
rid = seed_lost_and_reissue()
app = make_reissue_app(ReissueAction(kind='edit', reissue_id=rid, delivery_address_id='addr-work'),
                        classifier=lambda state: R_REISSUE(action='reject'))
cfg, r = start(app, 'reissue-reject')
r = app.invoke(Command(resume='아니야'), config=cfg)
data = data_store.load_data()
check('거절 시 변경 없음', data['reissue_applications'][0]['delivery_address_id'] == 'addr-home')


# ---------- 5. card_lost_and_reissue_agent (정지 후 재발급) ----------
def make_lr_app(card_id='card-001', address_id='addr-home'):
    def fake_identify(state):
        return {'pending_reissue_card_id': card_id}

    def fake_reissue_extract(state):
        return {'reissue_action': ReissueAction(kind='create', card_id=state['pending_reissue_card_id'], delivery_address_id=address_id)}

    builder = StateGraph(BankState)
    builder.add_node('lr_agent', build_card_lost_and_reissue_graph(
        identify=fake_identify, reissue_extract_fn=fake_reissue_extract,
    ))
    builder.add_edge(START, 'lr_agent')
    builder.add_edge('lr_agent', END)
    return builder.compile(checkpointer=InMemorySaver(serde=graph.checkpoint_serde))


fresh()
app = make_lr_app()
cfg, r = start(app, 'lr-full')
check('1단계: 분실 정지 승인 화면', '분실 정지' in screen(r))
check('정지 전 데이터 불변', status_of('card-001') == 'active')
r = app.invoke(Command(resume='승인'), config=cfg)
check('정지 승인 후 카드 상태 lost로 반영', status_of('card-001') == 'lost')
check('이어서 2단계: 재발급 승인 화면', '__interrupt__' in r and '재발급' in screen(r))
r = app.invoke(Command(resume='승인'), config=cfg)
data = data_store.load_data()
check('재발급 신청까지 완료', len(data['reissue_applications']) == 1 and data['reissue_applications'][0]['status'] == 'received')
check('완료 메시지', '신청 번호' in r['messages'][-1].content)

fresh()
original = raw()
app = make_lr_app()
cfg, r = start(app, 'lr-reject-lost')
r = app.invoke(Command(resume='거절'), config=cfg)
check('정지를 거절하면 재발급으로 넘어가지 않고 종료', '__interrupt__' not in r and raw() == original)

fresh()
lost_data, _ = functions.apply_card_status_change(data_store.load_data(), OWNER, 'card-001', 'lost')
data_store.save_data(lost_data)
app = make_lr_app()
cfg, r = start(app, 'lr-already-lost')
check('이미 분실 정지된 카드는 정지 단계 없이 바로 재발급 승인 화면', '__interrupt__' in r and '재발급' in screen(r))
r = app.invoke(Command(resume='승인'), config=cfg)
data = data_store.load_data()
check('재발급 신청 완료', len(data['reissue_applications']) == 1)

shutil.rmtree(TMP_DIR, ignore_errors=True)
print('\n모든 카드 테스트 통과')
