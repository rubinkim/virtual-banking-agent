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
from graph import BankState, NicknameChange, build_nickname_graph

TMP_DIR = Path(tempfile.mkdtemp())
data_store.DATA_PATH = TMP_DIR / 'data.json'


def fresh():
    shutil.copyfile(data_store.INITIAL_PATH, data_store.DATA_PATH)


def raw():
    return data_store.DATA_PATH.read_bytes()


def nickname_of(account_id):
    return next(a['nickname'] for a in data_store.load_data()['accounts'] if a['account_id'] == account_id)


def check(name, cond, detail=''):
    assert cond, f'FAIL - {name} {detail}'
    print('PASS -', name, detail)


OWNER = 'user-001'
# fresh() 기준: acc-001 생활비, acc-002 저축, acc-003 여행 자금

# ---------- 1. 검증 함수 ----------
fresh()
d = data_store.load_data()
v = lambda acc, name: functions.validate_nickname_change(d, OWNER, acc, name)
check('정상 변경은 통과', v('acc-001', '휴가비') is None)
check('앞뒤 공백 제거 후 판단', v('acc-001', '  휴가비  ') is None)
check('빈 문자열 거부', v('acc-001', '   ') is not None)
check('21자 이상 거부', v('acc-001', 'a' * 21) is not None)
check('20자는 허용', v('acc-001', 'a' * 20) is None)
check('같은 별명 거부', v('acc-001', '생활비') is not None)
check('다른 계좌와 중복되면 거부', v('acc-001', '저축') is not None)
check('남의 계좌 거부', v('acc-004', '아무거나') is not None)
check('없는 계좌 거부', v('acc-999', '아무거나') is not None)

# ---------- 2. 변경 함수 ----------
fresh()
before = data_store.load_data()
new_data, record = functions.apply_nickname_change(before, OWNER, 'acc-001', ' 휴가비 ')
check('원본 dict는 바뀌지 않음', nickname_of('acc-001') == '생활비')
check('별명이 바뀌고 공백은 제거됨', next(a['nickname'] for a in new_data['accounts'] if a['account_id'] == 'acc-001') == '휴가비')
check('처리 기록 생성', record['type'] == 'nickname_change' and record['old_nickname'] == '생활비'
      and record['new_nickname'] == '휴가비' and record['status'] == 'completed')
check('처리 기록이 requests에 추가됨', new_data['requests'][-1] == record)

# ---------- 3. 그래프 (LLM 없이: 추출 노드만 가짜) ----------
def make_app(account_id='acc-001', new_nickname='휴가비', classifier=None):
    def fake_extract(state):
        return {'nickname_change': NicknameChange(account_id=account_id, new_nickname=new_nickname)}

    builder = StateGraph(BankState)
    builder.add_node('nickname_agent', build_nickname_graph(fake_extract, classifier or graph.classify_nickname_response))
    builder.add_edge(START, 'nickname_agent')
    builder.add_edge('nickname_agent', END)
    return builder.compile(checkpointer=InMemorySaver(serde=graph.checkpoint_serde))


def start(app, tid):
    cfg = {'configurable': {'thread_id': tid}}
    r = app.invoke({'messages': [HumanMessage(content='별명 바꿔줘')], 'owner_id': OWNER}, config=cfg)
    return cfg, r


def screen(r):
    return r['__interrupt__'][0].value['message']


# 3-1 승인
fresh()
app = make_app()
cfg, r = start(app, 'approve')
check('승인 전 interrupt로 멈춤', '__interrupt__' in r)
check('승인 화면에 현재·새 별명 표시', '생활비(acc-001)' in screen(r) and '휴가비' in screen(r))
check('승인 대기 중에는 데이터 변경 없음', nickname_of('acc-001') == '생활비')
r = app.invoke(Command(resume='승인'), config=cfg)
check('승인 후 별명 반영', nickname_of('acc-001') == '휴가비')
check('완료 메시지', "'생활비'에서 '휴가비'" in r['messages'][-1].content)
check('종료 후 State 비워짐', app.get_state(cfg).values['nickname_change'] is None)

# 3-2 거절
fresh()
original = raw()
app = make_app()
cfg, r = start(app, 'reject')
r = app.invoke(Command(resume='거절'), config=cfg)
check('거절 시 파일이 한 바이트도 바뀌지 않음', raw() == original)
check('거절 메시지', '취소' in r['messages'][-1].content)

# 3-3 검증 실패는 승인 요청 없이 종료
for name, kw in [('남의 계좌', dict(account_id='acc-004')),
                 ('같은 별명', dict(new_nickname='생활비')),
                 ('중복 별명', dict(new_nickname='저축')),
                 ('빈 별명', dict(new_nickname='   '))]:
    fresh()
    original = raw()
    app = make_app(**kw)
    cfg, r = start(app, name)
    check(f'검증 실패({name}): 승인 요청 없이 종료', '__interrupt__' not in r and '바꿀 수 없습니다' in r['messages'][-1].content)
    check(f'검증 실패({name}): 데이터 변경 없음', raw() == original)

# 3-4 자연어 승인·거절
fresh()
app = make_app(classifier=lambda state: graph.NicknameResponseInterpretation(action='approve'))
cfg, r = start(app, 'nl-approve')
r = app.invoke(Command(resume='응, 그렇게 해줘'), config=cfg)
check('자연어 승인으로 변경 실행', nickname_of('acc-001') == '휴가비')

fresh()
original = raw()
app = make_app(classifier=lambda state: graph.NicknameResponseInterpretation(action='reject'))
cfg, r = start(app, 'nl-reject')
r = app.invoke(Command(resume='아니 됐어'), config=cfg)
check('자연어 거절은 변경 없이 종료', raw() == original and '취소' in r['messages'][-1].content)

# 3-5 애매한 응답은 다시 물음
fresh()
app = make_app(classifier=lambda state: graph.NicknameResponseInterpretation(action='unclear'))
cfg, r = start(app, 'unclear')
r = app.invoke(Command(resume='음...'), config=cfg)
check('애매하면 승인하지 않고 다시 화면', '__interrupt__' in r and '이해하지 못했습니다' in screen(r))
check('데이터 변경 없음', nickname_of('acc-001') == '생활비')
r = app.invoke(Command(resume='승인'), config=cfg)
check('다시 승인하면 실행', nickname_of('acc-001') == '휴가비')

# 3-6 승인 대기 중 다른 계좌가 같은 별명을 쓰게 되면 실행 직전 재검증에서 막힘
fresh()
app = make_app()
cfg, r = start(app, 'revalidate')
drained = data_store.load_data()
next(a for a in drained['accounts'] if a['account_id'] == 'acc-002')['nickname'] = '휴가비'
data_store.save_data(drained)
r = app.invoke(Command(resume='승인'), config=cfg)
check('재검증 실패 메시지', '승인 이후' in r['messages'][-1].content)
check('재검증 실패 시 데이터 변경 없음(대상 계좌는 그대로)', nickname_of('acc-001') == '생활비')

shutil.rmtree(TMP_DIR, ignore_errors=True)
print('\n모든 별명 변경 테스트 통과')
