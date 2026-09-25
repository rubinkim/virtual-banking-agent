# 사용자 입력을 반복해서 받고 그래프의 응답을 출력합니다.
# 같은 대화 세션을 유지하고 승인·거절 입력과 종료 명령을 처리합니다.

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from graph import parent_graph

OWNER_ID = "user-001"
config = {'configurable': {'thread_id': 'session-1'}}
QUIT_WORDS = ('quit', 'exit', '종료')


def ask_decision(payload: dict) -> str | None:
    """그래프가 interrupt로 멈췄을 때 사용자의 응답 원문을 받는다. 해석은 그래프가 한다. 종료를 입력하면 None을 반환한다."""
    print(f"\nBot: {payload['message']}")
    while True:
        answer = input('승인, 거절 또는 수정할 내용을 입력해 주세요: ').strip()
        if answer.lower() in QUIT_WORDS:
            return None
        if answer:
            return answer


print('은행 업무 에이전트 (종료: quit)')

while True:
    user_input = input('\n나: ').strip()
    if user_input.lower() in QUIT_WORDS:
        break
    if not user_input:
        continue

    result = parent_graph.invoke(
        {'messages': [HumanMessage(content=user_input)], 'owner_id': OWNER_ID},
        config=config,
    )

    quit_requested = False
    while result.get('__interrupt__'):
        decision = ask_decision(result['__interrupt__'][0].value)
        if decision is None:
            quit_requested = True
            break
        result = parent_graph.invoke(Command(resume=decision), config=config)

    if quit_requested:
        print('\n승인 대기 중이던 이체는 실행되지 않았습니다. 종료합니다.')
        break

    print(f"\nBot: {result['messages'][-1].text}")
